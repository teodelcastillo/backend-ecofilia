from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from django.db.models import QuerySet

from apps.chat.services.context_builder import build_context_block as _build_context_block
from apps.chat.services.rag import (
    RAG_MIN_SIMILARITY,
    _chunk_similarity,
    build_context_block,
    fetch_relevant_chunks,
)
from apps.chat.services.retrieval import lexical_search, rrf_fuse
from apps.document.models import SmartChunk

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_SEARCH_DOCUMENTS: dict = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Search the project's document corpus for relevant excerpts on a "
            "specific topic. Use this to find evidence, data, or statements "
            "before drafting content or answering questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Specific search query to find relevant document sections.",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Number of excerpts to retrieve (1–8). Default 4.",
                    "default": 4,
                },
            },
            "required": ["query"],
        },
    },
}

TOOL_CALCULATE_GHG: dict = {
    "type": "function",
    "function": {
        "name": "calculate_ghg_emissions",
        "description": (
            "Calculate greenhouse gas emissions from activity data and an emission factor. "
            "Returns the result in tCO2e. Use this instead of doing arithmetic in text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "activity_data": {
                    "type": "number",
                    "description": "Activity data value (e.g. kWh, litres, km).",
                },
                "emission_factor": {
                    "type": "number",
                    "description": "Emission factor in tCO2e per unit of activity data.",
                },
                "unit": {
                    "type": "string",
                    "description": "Unit of activity data (e.g. kWh, litres, km, tonnes).",
                },
                "scope": {
                    "type": "string",
                    "enum": ["scope_1", "scope_2", "scope_3", "unknown"],
                    "description": "GHG Protocol scope.",
                    "default": "unknown",
                },
                "description": {
                    "type": "string",
                    "description": "Brief label for the emission source.",
                    "default": "",
                },
            },
            "required": ["activity_data", "emission_factor", "unit"],
        },
    },
}

TOOL_GET_DOCUMENT_LIST: dict = {
    "type": "function",
    "function": {
        "name": "get_document_list",
        "description": (
            "Return the list of documents available in the project. "
            "Use this to understand what sources exist."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

TOOL_RUN_SKILL: dict = {
    "type": "function",
    "function": {
        "name": "run_skill",
        "description": (
            "Execute a quick skill/analysis on the project's documents and return "
            "the result. Use this to run predefined analyses like materiality "
            "assessments, gap analyses, or compliance checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_slug": {
                    "type": "string",
                    "description": "Slug of the skill to execute.",
                },
                "extra_instructions": {
                    "type": "string",
                    "description": "Optional additional instructions for the skill run.",
                    "default": "",
                },
            },
            "required": ["skill_slug"],
        },
    },
}

TOOL_GET_EXECUTION_HISTORY: dict = {
    "type": "function",
    "function": {
        "name": "get_execution_history",
        "description": (
            "Retrieve summaries of recent skill executions in this project. "
            "Use this to reference prior analyses or check what has been done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of recent executions to return (1–10). Default 5.",
                    "default": 5,
                },
                "skill_slug": {
                    "type": "string",
                    "description": "Optional: filter by skill slug.",
                },
            },
            "required": [],
        },
    },
}

TOOL_GET_PROJECT_STRUCTURE: dict = {
    "type": "function",
    "function": {
        "name": "get_project_structure",
        "description": (
            "Get the current project structure with section statuses. "
            "Use this to check progress or decide what to work on next."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

TOOL_UPDATE_SECTION_STATUS: dict = {
    "type": "function",
    "function": {
        "name": "update_section_status",
        "description": (
            "Update the status of a project section and optionally add notes. "
            "Use this after completing work on a section or when the consultant "
            "confirms progress."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section_position": {
                    "type": "integer",
                    "description": "Position number of the section to update.",
                },
                "status": {
                    "type": "string",
                    "enum": ["not_started", "in_progress", "review", "completed"],
                    "description": "New status for the section.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes to add to the section.",
                    "default": "",
                },
            },
            "required": ["section_position", "status"],
        },
    },
}

COPILOT_TOOLS: list[dict] = [
    TOOL_SEARCH_DOCUMENTS,
    TOOL_CALCULATE_GHG,
    TOOL_GET_DOCUMENT_LIST,
    TOOL_RUN_SKILL,
    TOOL_GET_EXECUTION_HISTORY,
    TOOL_GET_PROJECT_STRUCTURE,
    TOOL_UPDATE_SECTION_STATUS,
]


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------

@dataclass
class CopilotToolContext:
    user: Any
    project: Any
    allowed_documents: QuerySet
    additional_chunks: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Copilot retrieval helpers
# ---------------------------------------------------------------------------

def retrieve_for_copilot(
    *,
    user,
    project,
    allowed_documents: QuerySet,
    query_text: str,
    top_n: int = 12,
) -> list:
    """
    Hybrid retrieval for the Copilot search_documents tool.

    Blueprint chunks are retrieved first (up to 3) with a guaranteed slot so
    the project mandate is always visible to the LLM. The remaining budget is
    filled via RRF fusion of vector + lexical over the rest of the corpus.
    """
    blueprint_slug = (
        project.blueprint_document.slug if project.blueprint_document_id else None
    )

    blueprint_chunks: list = []
    if blueprint_slug:
        blueprint_doc_qs = allowed_documents.filter(slug=blueprint_slug)
        if blueprint_doc_qs.exists():
            try:
                blueprint_chunks = list(
                    fetch_relevant_chunks(
                        user=user,
                        query_text=query_text,
                        allowed_documents=blueprint_doc_qs,
                        top_n=3,
                        retrieval_strategy="global",
                        total_limit=3,
                    )
                )
            except Exception as exc:
                logger.warning("Copilot blueprint retrieval failed: %s", exc)

    other_docs = (
        allowed_documents.exclude(slug=blueprint_slug)
        if blueprint_slug
        else allowed_documents
    )
    remaining_budget = max(1, top_n - len(blueprint_chunks))

    other_doc_ids = list(other_docs.values_list("id", flat=True))
    if not other_doc_ids:
        return blueprint_chunks

    try:
        v_chunks = fetch_relevant_chunks(
            user=user,
            query_text=query_text,
            allowed_documents=other_docs,
            top_n=remaining_budget,
            retrieval_strategy="auto",
            total_limit=remaining_budget,
        )
    except Exception as exc:
        logger.warning("Copilot vector retrieval failed: %s", exc)
        v_chunks = []

    base_qs = SmartChunk.objects.filter(
        document_id__in=other_doc_ids
    ).exclude(embedding__isnull=True)
    try:
        lex_chunks = lexical_search(base_qs, query_text, top_n=remaining_budget)
    except Exception as exc:
        logger.warning("Copilot lexical retrieval failed: %s", exc)
        lex_chunks = []

    ranked_lists = [lst for lst in [v_chunks, lex_chunks] if lst]
    if ranked_lists:
        fused = rrf_fuse(ranked_lists, top_n=remaining_budget)
        fused = [
            c for c in fused
            if _chunk_similarity(c) is None or _chunk_similarity(c) >= RAG_MIN_SIMILARITY
        ]
    else:
        fused = []

    return blueprint_chunks + fused


def build_project_brief(user, project, documents: QuerySet) -> str:
    """
    High-level corpus overview generated once per Copilot session.

    Injected as ambient system context so the LLM has a baseline understanding
    of the project's documents from the very first message, before any tool
    calls. Uses ``with_citations=False`` so [#N] markers don't leak into
    system-level context that has no corresponding user message.
    """
    if not documents.exists():
        return ""

    broad_query = " ".join(filter(None, [
        project.name,
        project.description or "",
        "objetivos compromisos metodología emisiones",
    ])).strip()

    chunks = retrieve_for_copilot(
        user=user,
        project=project,
        allowed_documents=documents,
        query_text=broad_query,
        top_n=8,
    )
    if not chunks:
        return ""

    return _build_context_block(chunks, with_citations=False)


# ---------------------------------------------------------------------------
# Individual tool executors
# ---------------------------------------------------------------------------

def _execute_search_documents(args: dict, ctx: CopilotToolContext) -> str:
    from apps.project.models import ProjectSection

    query = (args.get("query") or "").strip()
    if not query:
        return "Error: 'query' parameter is required."

    top_n = min(max(int(args.get("top_n", 4)), 1), 8)

    # Phase 2: enrich query with the active section's context so retrieval
    # pulls toward the part of the corpus most relevant to current work.
    active_section = (
        ProjectSection.objects.filter(project=ctx.project, status="in_progress")
        .order_by("position")
        .first()
    )
    enriched_query = query
    if active_section and active_section.description:
        enriched_query = (
            f"{query}. Contexto: {active_section.title}. "
            f"{active_section.description[:200]}"
        ).strip()

    try:
        chunks = retrieve_for_copilot(
            user=ctx.user,
            project=ctx.project,
            allowed_documents=ctx.allowed_documents,
            query_text=enriched_query,
            top_n=top_n,
        )
    except Exception as exc:
        logger.warning("search_documents tool failed: %s", exc)
        return f"Search failed: {exc}"

    ctx.additional_chunks.extend(chunks)
    if not chunks:
        return "No relevant excerpts found for this query."
    return build_context_block(chunks)


def _execute_calculate_ghg(args: dict, ctx: CopilotToolContext) -> str:
    try:
        activity_data = float(args["activity_data"])
        emission_factor = float(args["emission_factor"])
        unit = str(args.get("unit", ""))
        scope = str(args.get("scope", "unknown"))
        description = str(args.get("description", ""))
    except (KeyError, ValueError, TypeError) as exc:
        return (
            f"Calculation error: {exc}. "
            "Required: activity_data (number), emission_factor (number), unit (string)."
        )

    result_tco2e = activity_data * emission_factor
    lines = [
        "GHG Calculation Result:",
        f"- Activity data: {activity_data:,.4g} {unit}",
        f"- Emission factor: {emission_factor:,.6g} tCO2e/{unit}",
        f"- Result: {result_tco2e:,.4g} tCO2e",
        f"- Scope: {scope.replace('_', ' ').title()}",
    ]
    if description:
        lines.append(f"- Source: {description}")
    return "\n".join(lines)


def _execute_get_document_list(args: dict, ctx: CopilotToolContext) -> str:
    try:
        docs = list(ctx.allowed_documents.only("id", "name", "slug"))
    except Exception as exc:
        logger.warning("get_document_list tool failed: %s", exc)
        return f"Failed to retrieve document list: {exc}"

    if not docs:
        return "No documents available in the project."
    lines = [f"Available documents ({len(docs)}):"]
    for doc in docs:
        lines.append(f"- [{doc.slug}] {doc.name}")
    return "\n".join(lines)


def _execute_run_skill(args: dict, ctx: CopilotToolContext) -> str:
    from apps.skill.models import Skill, SkillExecution, SkillType, ExecutionStatus
    from apps.skill.services import execute_skill

    skill_slug = (args.get("skill_slug") or "").strip()
    if not skill_slug:
        return "Error: 'skill_slug' parameter is required."

    try:
        skill = Skill.objects.get(slug=skill_slug)
    except Skill.DoesNotExist:
        return f"Skill '{skill_slug}' not found."

    # Las mismas reglas que el botón de ejecutar: un restringido no ve lo que
    # no está asignado a su organización, y ejecutar lo decide el asistente.
    # Sin esto el chat era una puerta lateral a cualquier skill por su slug.
    from apps.skill.access import RUN_FORBIDDEN_MESSAGE, user_can_run_skill

    user = ctx.user
    if getattr(user, "is_restricted", False) and not skill.enabled_for_organizations.filter(
        pk=user.organization_id
    ).exists():
        return f"Skill '{skill_slug}' not found."
    if not user_can_run_skill(user, skill):
        return RUN_FORBIDDEN_MESSAGE

    if skill.skill_type != SkillType.QUICK:
        return (
            f"Skill '{skill.name}' is a copilot (multi-step) skill and cannot be run "
            "inline. Only quick skills can be executed from the copilot chat."
        )

    extra_instructions = (args.get("extra_instructions") or "").strip()

    try:
        execution = SkillExecution.objects.create(
            skill=skill,
            owner=ctx.user,
            project=ctx.project,
            extra_instructions=extra_instructions,
        )
        result = execute_skill(execution)
    except Exception as exc:
        logger.warning("run_skill tool failed for %s: %s", skill_slug, exc)
        return f"Skill execution failed: {exc}"

    if result.status == ExecutionStatus.FAILED:
        return f"Skill '{skill.name}' failed: {result.error_message}"

    output = result.output or ""
    if len(output) > 3000:
        output = output[:3000] + "\n... (output truncated)"
    return f"Skill '{skill.name}' completed:\n\n{output}"


def _execute_get_execution_history(args: dict, ctx: CopilotToolContext) -> str:
    from apps.skill.models import SkillExecution

    limit = min(max(int(args.get("limit", 5)), 1), 10)
    skill_slug = (args.get("skill_slug") or "").strip()

    qs = SkillExecution.objects.filter(
        project=ctx.project, owner=ctx.user,
    ).select_related("skill").order_by("-created_at")

    if skill_slug:
        qs = qs.filter(skill__slug=skill_slug)

    executions = list(qs[:limit])
    if not executions:
        return "No skill executions found for this project."

    lines = [f"Recent executions ({len(executions)}):"]
    for ex in executions:
        preview = (ex.output or "")[:200]
        if len(ex.output or "") > 200:
            preview += "..."
        lines.append(
            f"- [{ex.status}] {ex.skill.name} (id={ex.id}, "
            f"{ex.created_at.strftime('%Y-%m-%d %H:%M')}): {preview}"
        )
    return "\n".join(lines)


def _execute_get_project_structure(args: dict, ctx: CopilotToolContext) -> str:
    from apps.project.models import ProjectSection

    sections = list(
        ProjectSection.objects.filter(project=ctx.project).order_by("position")
    )
    if not sections:
        return "This project has no structure defined."

    status_labels = {
        "not_started": "PENDIENTE",
        "in_progress": "EN PROGRESO",
        "review": "EN REVISION",
        "completed": "COMPLETADO",
    }
    lines = ["Project structure:"]
    for s in sections:
        label = status_labels.get(s.status, s.status.upper())
        line = f"{s.position}. [{label}] {s.title}"
        if s.notes:
            line += f" — Notes: {s.notes}"
        lines.append(line)
    return "\n".join(lines)


def _execute_update_section_status(args: dict, ctx: CopilotToolContext) -> str:
    from apps.project.models import ProjectSection, ProjectSectionStatus

    position = args.get("section_position")
    new_status = (args.get("status") or "").strip()
    notes = (args.get("notes") or "").strip()

    if position is None:
        return "Error: 'section_position' is required."

    valid_statuses = {c.value for c in ProjectSectionStatus}
    if new_status not in valid_statuses:
        return f"Error: invalid status '{new_status}'. Valid: {', '.join(valid_statuses)}"

    try:
        section = ProjectSection.objects.get(
            project=ctx.project, position=int(position),
        )
    except ProjectSection.DoesNotExist:
        return f"No section at position {position} in this project."

    section.status = new_status
    update_fields = ["status"]
    if notes:
        section.notes = notes
        update_fields.append("notes")
    section.save(update_fields=update_fields)

    return f"Section '{section.title}' updated to '{new_status}'."


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_COPILOT_TOOL_EXECUTORS: dict[str, Callable[[dict, CopilotToolContext], str]] = {
    "search_documents": _execute_search_documents,
    "calculate_ghg_emissions": _execute_calculate_ghg,
    "get_document_list": _execute_get_document_list,
    "run_skill": _execute_run_skill,
    "get_execution_history": _execute_get_execution_history,
    "get_project_structure": _execute_get_project_structure,
    "update_section_status": _execute_update_section_status,
}


def execute_copilot_tool(tool_name: str, tool_args_json: str, ctx: CopilotToolContext) -> str:
    executor = _COPILOT_TOOL_EXECUTORS.get(tool_name)
    if executor is None:
        return f"Unknown tool '{tool_name}'. Available: {', '.join(_COPILOT_TOOL_EXECUTORS)}."
    try:
        args = json.loads(tool_args_json) if tool_args_json else {}
    except json.JSONDecodeError:
        return f"Invalid JSON arguments for tool '{tool_name}'."
    return executor(args, ctx)
