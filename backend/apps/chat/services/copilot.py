from __future__ import annotations

import logging
import os
import re
from typing import List

from django.core.cache import cache
from django.db.models import QuerySet

from apps.chat.models import ChatMessage, ChatSession, MessageRole
from apps.chat.services.copilot_tools import (
    COPILOT_TOOLS,
    CopilotToolContext,
    build_project_brief,
    execute_copilot_tool,
)
from apps.chat.services.rag import suggest_related_library_documents
from apps.document.models import Document
from apps.document.utils.client_openia import generate_chat_completion, generate_with_tools
from apps.document.utils.llm import tool_capable_model
from apps.project.models import Project, ProjectSection

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = int(os.environ.get("COPILOT_HISTORY_MESSAGES", "20"))
# When total history exceeds this threshold, older messages are compressed into
# a summary block injected as system context. Keeps the window coherent without
# losing context from the start of long sessions.
HISTORY_SUMMARY_THRESHOLD = int(os.environ.get("COPILOT_HISTORY_SUMMARY_THRESHOLD", "25"))

# El resumen del proyecto se arma una vez por sesión y se reusa en los mensajes
# siguientes. Vivía en ``session.metadata``, un campo que ``ChatSession`` nunca
# tuvo (``metadata`` es de ``ChatMessage``): desde mayo de 2026 cada mensaje al
# copilot caía con AttributeError y la API respondía 503. Va al caché y no a una
# columna porque es derivable: si se pierde, o el pedido cae en otro proceso, se
# vuelve a calcular.
PROJECT_BRIEF_CACHE_SECONDS = int(os.environ.get("COPILOT_BRIEF_CACHE_SECONDS", str(6 * 3600)))


def _project_brief_cache_key(session: ChatSession) -> str:
    return f"copilot:project_brief:{session.pk}"

# Delimiters used to wrap draft content the consultant can insert into the
# editor as a clean section body. The optional `section=N` attribute lets the
# model bind the draft to a specific ProjectSection.position.
DRAFT_OPEN_PATTERN = re.compile(
    r"<<<DRAFT(?:\s+section=(?P<position>\d+))?>>>",
    flags=re.IGNORECASE,
)
DRAFT_CLOSE_TOKEN = "<<<END>>>"


# ---------------------------------------------------------------------------
# Document resolver for project context
# ---------------------------------------------------------------------------

def _resolve_project_documents(project: Project) -> QuerySet[Document]:
    from apps.project.models import ProjectDocument

    doc_ids = (
        ProjectDocument.objects
        .filter(project_id=project.id)
        .values_list("document_id", flat=True)
    )
    return Document.objects.filter(id__in=doc_ids)


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _format_context_notes(context_notes: dict) -> str:
    if not context_notes:
        return ""
    lines = ["## Contexto del proyecto"]
    for key, value in context_notes.items():
        if value:
            label = key.replace("_", " ").title()
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def _format_project_structure(project: Project) -> str:
    sections = list(
        ProjectSection.objects.filter(project=project).order_by("position")
    )
    if not sections:
        return ""

    status_labels = {
        "not_started": "PENDIENTE",
        "in_progress": "EN PROGRESO",
        "review": "EN REVISION",
        "completed": "COMPLETADO",
    }

    template_name = ""
    if project.structure_template:
        template_name = f' "{project.structure_template.name}"'

    lines = [f"## Estructura del proyecto{template_name}"]
    for s in sections:
        label = status_labels.get(s.status, s.status.upper())
        line = f"{s.position}. [{label}] {s.title}"
        if s.description:
            line += f" — {s.description}"
        if s.notes:
            line += f" (Notas: {s.notes})"
        lines.append(line)
    return "\n".join(lines)


def _format_document_list(
    documents: QuerySet[Document],
    *,
    blueprint_slug: str | None = None,
) -> str:
    docs = list(documents.only("name", "slug"))
    if not docs:
        return "## Documentos disponibles\nNo hay documentos en el proyecto."
    lines = [f"## Documentos disponibles ({len(docs)})"]
    for doc in docs:
        marker = " [BLUEPRINT]" if blueprint_slug and doc.slug == blueprint_slug else ""
        lines.append(f"- [{doc.slug}]{marker} {doc.name}")
    return "\n".join(lines)


def build_copilot_system_prompt(
    project: Project,
    documents: QuerySet[Document],
) -> str:
    parts = [
        (
            f'Eres el Copiloto Ecofilia, un asistente experto en sostenibilidad que guia '
            f'al consultor a traves del proyecto "{project.name}".\n\n'
            "Tu rol:\n"
            "- Propones y sugieres, pero el consultor dirige.\n"
            "- Cuando el consultor pida avanzar, sugiere la siguiente seccion pendiente.\n"
            "- Puedes buscar en los documentos del proyecto, ejecutar analisis (skills), "
            "y calcular emisiones usando tus herramientas.\n"
            "- Cita siempre las fuentes de los documentos cuando uses informacion de ellos.\n"
            "- Responde en espanol a menos que el consultor use otro idioma."
        ),
        (
            "## Borradores para el documento final\n"
            "Cuando produzcas contenido pensado para INSERTAR en el documento entregable, "
            "envuelvelo entre los delimitadores <<<DRAFT>>> y <<<END>>>. Si el borrador "
            "corresponde a una seccion concreta de la estructura, indica la posicion en la "
            "etiqueta de apertura: <<<DRAFT section=N>>>. Solo el contenido entre delimitadores "
            "se insertara como cuerpo del entregable: usa Markdown (titulos con #, listas, "
            "negritas, etc.) y omite frases conversacionales como 'aqui tienes' o 'espero que "
            "te sirva'. Fuera de los delimitadores se breve, conversacional y orientado a "
            "siguiente paso. Si el consultor pide solo una idea o discusion, no uses los "
            "delimitadores."
        ),
    ]

    context_notes_block = _format_context_notes(project.context_notes or {})
    if context_notes_block:
        parts.append(context_notes_block)

    structure_block = _format_project_structure(project)
    if structure_block:
        parts.append(structure_block)

    blueprint_slug = (
        project.blueprint_document.slug if project.blueprint_document_id else None
    )
    doc_list_block = _format_document_list(documents, blueprint_slug=blueprint_slug)
    parts.append(doc_list_block)

    if blueprint_slug:
        parts.append(
            "## Documento principal (blueprint)\n"
            f"El blueprint del proyecto es '{blueprint_slug}'. Tratalo como mandato del "
            "encargo: las decisiones del entregable deben respetar lo definido alli, y "
            "cuando hagas afirmaciones del proyecto cita primero el blueprint cuando aplique."
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# History summarization
# ---------------------------------------------------------------------------

def _summarize_history(messages: list[dict], project_name: str) -> str:
    """
    Compress a list of older conversation messages into a compact summary.

    Called only when the total session history exceeds HISTORY_SUMMARY_THRESHOLD
    so that the copilot retains context about decisions made early in a long
    session without blowing up the context window.
    """
    if not messages:
        return ""

    conversation_text = "\n".join(
        f"{m['role'].upper()}: {(m['content'] or '')[:600]}"
        for m in messages
    )

    summary_messages = [
        {
            "role": "system",
            "content": (
                "Eres un asistente que resume conversaciones de trabajo. "
                "Produce un resumen compacto (máximo 250 palabras) de los puntos clave, "
                "decisiones tomadas, y contexto relevante de esta conversación. "
                "Omite saludos, afirmaciones triviales, y conversación genérica. "
                "Responde solo con el resumen, sin preámbulo."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Proyecto: {project_name}\n\n"
                f"Conversación a resumir:\n{conversation_text}"
            ),
        },
    ]

    try:
        summary, _ = generate_chat_completion(
            summary_messages,
            temperature=0.1,
            max_tokens=350,
        )
        return summary
    except Exception as exc:
        logger.warning("History summarization failed, proceeding without summary: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Message composition
# ---------------------------------------------------------------------------

def build_copilot_messages(
    session: ChatSession,
    user_content: str,
    project: Project,
    documents: QuerySet[Document],
    *,
    max_history: int = MAX_HISTORY_MESSAGES,
    project_brief: str = "",
) -> list[dict]:
    system_prompt = build_copilot_system_prompt(project, documents)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]

    if project_brief:
        messages.append({
            "role": "system",
            "content": (
                "## Resumen inicial del corpus del proyecto\n"
                "Contexto pre-recuperado al inicio de la sesión. "
                "Úsalo para responder preguntas generales y para saber qué buscar "
                "con search_documents cuando necesites más detalle.\n\n"
                f"{project_brief}"
            ),
        })

    # Fetch the full session history (oldest first) excluding system messages.
    all_history = list(
        session.messages.order_by("created_at")
        .exclude(role=MessageRole.SYSTEM)
        .values("role", "content")
    )

    total = len(all_history)
    if total > HISTORY_SUMMARY_THRESHOLD:
        # Split: older messages get summarized, recent ones are kept verbatim.
        # We keep max_history recent messages so the immediate context is intact.
        keep_count = min(max_history, total)
        older = all_history[: total - keep_count]
        recent = all_history[total - keep_count :]

        summary = _summarize_history(older, project.name)
        if summary:
            messages.append({
                "role": "system",
                "content": (
                    "## Resumen de conversación anterior\n"
                    f"{summary}"
                ),
            })
        history_messages = [
            {"role": str(m["role"]), "content": m["content"] or ""}
            for m in recent
        ]
    else:
        # Session is within the normal window — include everything.
        history_messages = [
            {"role": str(m["role"]), "content": m["content"] or ""}
            for m in all_history[-max_history:]
        ]

    messages.extend(history_messages)
    messages.append({"role": "user", "content": user_content})
    return messages


# ---------------------------------------------------------------------------
# Draft extraction
# ---------------------------------------------------------------------------

def extract_draft_block(text: str) -> tuple[str | None, int | None]:
    """
    Pull the first ``<<<DRAFT[ section=N]>>> ... <<<END>>>`` block out of the
    assistant reply. Returns ``(draft_markdown, section_position)`` where each
    field is ``None`` when missing. Whitespace around the block is trimmed.
    """
    if not text:
        return None, None

    match = DRAFT_OPEN_PATTERN.search(text)
    if not match:
        return None, None

    body_start = match.end()
    end_idx = text.find(DRAFT_CLOSE_TOKEN, body_start)
    if end_idx == -1:
        return None, None

    body = text[body_start:end_idx].strip("\n").strip()
    if not body:
        return None, None

    position_str = match.group("position")
    position = int(position_str) if position_str else None
    return body, position


# ---------------------------------------------------------------------------
# Main copilot message processor
# ---------------------------------------------------------------------------

def process_copilot_message(
    session: ChatSession,
    user_content: str,
    user,
) -> tuple[str, dict, list[int]]:
    """
    Process a copilot message through the tool-calling loop.

    Returns (response_text, metadata, chunk_ids).
    """
    project = session.project
    if project is None:
        raise ValueError("Copilot sessions require a project context.")

    documents = _resolve_project_documents(project)

    # Phase 3: project brief — compute once per session and cache it so
    # subsequent messages don't pay the retrieval cost again.
    brief_key = _project_brief_cache_key(session)
    project_brief = cache.get(brief_key) or ""
    if not project_brief:
        try:
            project_brief = build_project_brief(user, project, documents)
            if project_brief:
                cache.set(brief_key, project_brief, PROJECT_BRIEF_CACHE_SECONDS)
        except Exception as exc:
            logger.warning("Copilot project brief failed, proceeding without it: %s", exc)
            project_brief = ""

    messages = build_copilot_messages(
        session, user_content, project, documents, project_brief=project_brief,
    )

    tool_ctx = CopilotToolContext(
        user=user,
        project=project,
        allowed_documents=documents,
    )

    def _tool_executor(name: str, args_json: str) -> str:
        return execute_copilot_tool(name, args_json, tool_ctx)

    # Copilot drives OpenAI tool-use; Anthropic's tool protocol isn't ported
    # yet, so pin to a tool-capable OpenAI model even under LLM_PROVIDER=anthropic.
    model = tool_capable_model(session.model)
    temperature = session.temperature

    response_text, usage = generate_with_tools(
        messages,
        tools=COPILOT_TOOLS,
        tool_executor=_tool_executor,
        model=model,
        temperature=temperature,
    )

    chunk_ids = [c.id for c in tool_ctx.additional_chunks if hasattr(c, "id")]

    draft_markdown, draft_section_position = extract_draft_block(response_text)

    metadata: dict = {
        "usage": usage,
        "copilot": True,
        "tools_used": len(tool_ctx.additional_chunks) > 0,
        "chunks_retrieved": len(tool_ctx.additional_chunks),
    }
    try:
        recs = suggest_related_library_documents(
            user=user,
            query_text=user_content,
            exclude_document_ids=[d.id for d in documents],
        )
        if recs:
            metadata["recommended_documents"] = recs
    except Exception as rec_exc:
        logger.warning("Copilot: recomendaciones de biblioteca omitidas: %s", rec_exc)
    if draft_markdown is not None:
        metadata["draft_markdown"] = draft_markdown
        metadata["draft_section_position"] = draft_section_position
        # Persist a snapshot on the section so the structure tab can show progress.
        # Scope the lookup to the deliverable the copilot session is tied
        # to, so multi-deliverable projects don't collide on position.
        if draft_section_position is not None:
            section_qs = ProjectSection.objects.filter(
                project=project, position=draft_section_position,
            )
            if session.deliverable_id is not None:
                section_qs = section_qs.filter(deliverable_id=session.deliverable_id)
            section = section_qs.first()
            if section is not None:
                section.output_snapshot = draft_markdown
                section.save(update_fields=["output_snapshot", "updated_at"])

    return response_text, metadata, chunk_ids


# ---------------------------------------------------------------------------
# Initialize project structure from template
# ---------------------------------------------------------------------------

def initialize_project_structure(
    project: Project,
    template_slug: str,
    *,
    deliverable=None,
) -> list[ProjectSection]:
    """
    Wipe and re-create the sections of a deliverable from a template.

    When ``deliverable`` is given (preferred path) the operation is scoped
    to that deliverable. For backward-compat with the legacy ``/structure``
    endpoint, ``deliverable=None`` resolves to the project's primary
    deliverable (auto-created if missing) so existing callers keep working.
    """
    from apps.project.models import ProjectDeliverable, ProjectStructureTemplate

    template = ProjectStructureTemplate.objects.get(slug=template_slug)

    if deliverable is None:
        deliverable = (
            ProjectDeliverable.objects.filter(project=project, is_primary=True).first()
            or ProjectDeliverable.objects.filter(project=project).first()
        )
        if deliverable is None:
            deliverable = ProjectDeliverable.objects.create(
                project=project,
                name="Entregable principal",
                template=template,
                is_primary=True,
                position=1,
            )

    ProjectSection.objects.filter(deliverable=deliverable).delete()

    # Keep the project-level template pointer in sync only for the primary
    # deliverable so the legacy field still represents "the" project template.
    deliverable.template = template
    deliverable.save(update_fields=["template", "updated_at"])
    if deliverable.is_primary:
        project.structure_template = template
        project.save(update_fields=["structure_template"])

    sections = []
    for ts in template.sections.order_by("position"):
        section = ProjectSection.objects.create(
            project=project,
            deliverable=deliverable,
            template_section=ts,
            title=ts.title,
            description=ts.description,
            position=ts.position,
        )
        sections.append(section)

    return sections


# ---------------------------------------------------------------------------
# Inline copilot autocomplete (editor ghost-text)
# ---------------------------------------------------------------------------

# Hard caps to keep autocomplete cheap and snappy. The editor sends short
# windows around the caret so we don't blow up the prompt context per
# keystroke / pause.
AUTOCOMPLETE_BEFORE_CHARS = 2000
AUTOCOMPLETE_AFTER_CHARS = 500
AUTOCOMPLETE_DOC_SUMMARY_CHARS = 400
AUTOCOMPLETE_MAX_DOCS = 6
AUTOCOMPLETE_MAX_TOKENS = 120


def _format_doc_briefs_for_autocomplete(documents: QuerySet[Document]) -> str:
    docs = list(documents.only("name", "slug", "content_summary")[: AUTOCOMPLETE_MAX_DOCS])
    if not docs:
        return ""
    lines = ["## Fuentes del proyecto"]
    for doc in docs:
        summary = (doc.content_summary or "").strip()
        if len(summary) > AUTOCOMPLETE_DOC_SUMMARY_CHARS:
            summary = summary[: AUTOCOMPLETE_DOC_SUMMARY_CHARS - 1].rstrip() + "…"
        if summary:
            lines.append(f"- {doc.name}: {summary}")
        else:
            lines.append(f"- {doc.name}")
    return "\n".join(lines)


def _build_autocomplete_system_prompt(
    project: Project,
    documents: QuerySet[Document],
    section: ProjectSection | None,
    doc_title: str | None,
) -> str:
    blueprint_slug = (
        project.blueprint_document.slug if project.blueprint_document_id else None
    )
    parts: list[str] = [
        (
            "Eres un motor de autocompletado contextual para un consultor de "
            "sostenibilidad y finanzas climaticas que esta redactando un documento. "
            "Tu tarea: continuar el texto donde quedo el cursor con 1 a 3 oraciones "
            "breves, coherentes con el tono y el idioma del autor, y consistentes con "
            "las fuentes del proyecto. No introduzcas datos inventados; si no hay "
            "evidencia clara, propone un puente o pregunta abierta."
        ),
        (
            "Formato de salida estricto:\n"
            "- Devuelve SOLO la continuacion directa del texto, sin comillas, sin "
            "preambulo, sin 'Aqui tienes', sin explicaciones.\n"
            "- No repitas la ultima frase del usuario; conectate suavemente con ella.\n"
            "- Maximo ~3 oraciones (~60 palabras).\n"
            "- Conserva el idioma exacto del texto de entrada.\n"
            "- Si el contexto despues del cursor empieza con una frase coherente, "
            "asegurate de que tu continuacion conecte gramaticalmente con ella."
        ),
        f'Proyecto: "{project.name}".',
    ]
    if project.description:
        parts.append(f"Resumen del proyecto: {project.description}")

    notes_block = _format_context_notes(project.context_notes or {})
    if notes_block:
        parts.append(notes_block)

    docs_block = _format_doc_briefs_for_autocomplete(documents)
    if docs_block:
        parts.append(docs_block)

    if blueprint_slug:
        parts.append(
            f"Documento principal (blueprint): '{blueprint_slug}'. Es el mandato del "
            "encargo: alinea las afirmaciones con lo descrito ahi."
        )

    if section is not None:
        section_block = [f"## Seccion en curso\n#{section.position}. {section.title}"]
        if section.description:
            section_block.append(f"Proposito: {section.description}")
        if section.notes:
            section_block.append(f"Notas internas: {section.notes}")
        parts.append("\n".join(section_block))

    if doc_title:
        parts.append(f"Documento de trabajo: '{doc_title}'.")

    return "\n\n".join(parts)


def generate_copilot_autocomplete(
    project: Project,
    *,
    before: str,
    after: str = "",
    section: ProjectSection | None = None,
    doc_title: str | None = None,
) -> tuple[str, dict]:
    """
    Generate an inline continuation for the editor caret position.

    Returns (completion_text, usage_dict). The completion is a short
    natural-language continuation of ``before``, optionally connecting into
    ``after`` if provided. We use a plain chat completion (no tools, no RAG
    streaming) to keep latency low — context comes from the project's stored
    document summaries.
    """
    before_text = (before or "")[-AUTOCOMPLETE_BEFORE_CHARS:]
    after_text = (after or "")[:AUTOCOMPLETE_AFTER_CHARS]

    documents = _resolve_project_documents(project)
    system_prompt = _build_autocomplete_system_prompt(
        project, documents, section, doc_title,
    )

    user_parts = ["CONTEXTO ANTES DEL CURSOR:\n" + before_text]
    if after_text.strip():
        user_parts.append("CONTEXTO DESPUES DEL CURSOR:\n" + after_text)
    user_parts.append("Continua el texto justo despues del cursor.")
    user_content = "\n\n".join(user_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    completion_text, usage = generate_chat_completion(
        messages,
        temperature=0.3,
        max_tokens=AUTOCOMPLETE_MAX_TOKENS,
    )

    cleaned = _clean_autocomplete_text(completion_text, before_text)
    return cleaned, usage


def _clean_autocomplete_text(text: str, before: str) -> str:
    """Trim quotes/preamble and any duplication of the trailing user text."""
    if not text:
        return ""
    out = text.strip()
    # Strip wrapping quotes added by some models.
    if (out.startswith('"') and out.endswith('"')) or (
        out.startswith("'") and out.endswith("'")
    ):
        out = out[1:-1].strip()
    if out.startswith("```"):
        out = out.lstrip("`").strip()

    # If the model echoed the end of `before`, drop the overlap so the inserted
    # text reads naturally when appended at the caret.
    tail = before[-200:].rstrip()
    if tail:
        for overlap_len in range(min(len(tail), len(out)), 10, -1):
            if out.startswith(tail[-overlap_len:]):
                out = out[overlap_len:].lstrip()
                break

    return out
