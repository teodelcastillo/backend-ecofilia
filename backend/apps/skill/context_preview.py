"""
Qué recibe cada paso de un workflow, calculado sin ejecutarlo.

Esto vivía dentro del comando ``preview_workflow_context``, que era el único
lugar donde se podía ver el presupuesto documental de una corrida: había que
entrar a ECS para responder una pregunta que el autor del workflow se hace
mientras lo escribe. Al necesitarlo también desde el builder, el cálculo sube
acá y el comando pasa a ser una de sus dos vistas.

No se reimplementa nada: arma el mismo corpus que armaría la corrida, con
``build_step_corpus``. Un presupuesto calculado aparte del motor diverge del
motor, y un presupuesto que miente es peor que no tenerlo.

Dos cosas que no hace, a propósito: no llama al modelo y no escribe en la base
—la ejecución que usa para simular nunca se guarda—.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from apps.skill import context_budget
from apps.skill.models import SkillExecution, SkillStep, StepEvidenceMode

logger = logging.getLogger(__name__)

PREVIEW_SCHEMA = 1

# Escritura de caché 1,25× · lectura 0,1×.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


@dataclass
class DocumentPreview:
    slug: str
    name: str
    mode: str
    tokens: int
    full_tokens: int
    is_blueprint: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "mode": self.mode,
            "tokens": self.tokens,
            "full_tokens": self.full_tokens,
            "is_blueprint": self.is_blueprint,
            "reason": self.reason,
        }


@dataclass
class StepPreview:
    position: int
    title: str
    tier: str
    tier_source: str
    model: str
    evidence_mode: str
    reads_documents: bool
    reserved_tokens: int
    cacheable_tokens: int
    # ``None`` —y no 0— cuando no se midieron los fragmentos: "no lo sabemos"
    # y "no ocupa nada" son respuestas distintas, y confundirlas es cómo un
    # presupuesto empieza a mentir.
    variable_tokens: int | None
    total_tokens: int
    exceeds_window: bool
    documents: list[DocumentPreview] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "position": self.position,
            "title": self.title,
            "tier": self.tier,
            "tier_source": self.tier_source,
            "model": self.model,
            "evidence_mode": self.evidence_mode,
            "reads_documents": self.reads_documents,
            "reserved_tokens": self.reserved_tokens,
            "cacheable_tokens": self.cacheable_tokens,
            "variable_tokens": self.variable_tokens,
            "total_tokens": self.total_tokens,
            "exceeds_window": self.exceeds_window,
            "documents": [d.as_dict() for d in self.documents],
        }


@dataclass
class WorkflowPreview:
    skill: dict
    project: dict
    window: dict
    steps: list[StepPreview]
    cache: dict
    warnings: list[str]
    fragments_measured: bool

    def as_dict(self) -> dict:
        return {
            "schema": PREVIEW_SCHEMA,
            "skill": self.skill,
            "project": self.project,
            "window": self.window,
            "fragments_measured": self.fragments_measured,
            "steps": [s.as_dict() for s in self.steps],
            "cache": self.cache,
            "warnings": self.warnings,
        }


def simulated_execution(skill, project) -> SkillExecution:
    """Una ejecución para simular, nunca persistida.

    Los workflows provistos por Ecofilia no tienen dueño (``owner=None``
    significa plantilla visible para todos), así que el dueño sale de la
    operación. Sin esto la recuperación dentro de los documentos degradados
    falla y el preview mide una parte variable que no existe.
    """
    owner = skill.owner or project.owner
    if owner is None:
        raise ValueError(
            "Ni el workflow ni la operación tienen dueño; la recuperación "
            "necesita un usuario para resolver permisos."
        )
    return SkillExecution(skill=skill, owner=owner, project=project, metadata={})


def _cache_report(cacheable: list[int], variable: list[int]) -> dict:
    """Qué se paga una vez y qué se paga en cada paso.

    Es la comprobación de que el punto de caché quedó donde tiene que quedar:
    si la parte estable no es idéntica en todos los pasos, el prefijo se rompe
    en el primer byte distinto y el corpus se cobra entero de nuevo. Por eso se
    compara y se avisa, en vez de asumirlo.

    Cuenta la reserva del paso —system incluido— como no cacheada, aunque el
    system viaje dentro del prefijo cacheado y en la práctica también se
    amortice. Es deliberado: subestima el ahorro en vez de prometerlo, y
    mantiene el número comparable con el que viene reportando el comando desde
    que se midió la línea base.
    """
    if not cacheable:
        return {"measurable": False, "reason": "no_steps"}

    distinct = sorted(set(cacheable))
    if len(distinct) > 1:
        return {
            "measurable": False,
            "reason": "unstable_prefix",
            "stable_identical": False,
            "distinct_sizes": distinct,
        }

    steps = len(cacheable)
    stable = cacheable[0]
    if steps < 2:
        # Con un solo paso la caché sólo se escribe: cuesta 1,25× y no hay
        # lecturas que lo amorticen. Informar un "ahorro" negativo confunde —
        # no dice nada del esquema, dice que se pidió un paso.
        return {
            "measurable": False,
            "reason": "single_step",
            "stable_identical": True,
            "cacheable_tokens": stable,
        }

    total_variable = sum(variable)
    with_cache = (
        stable * CACHE_WRITE_MULTIPLIER
        + stable * CACHE_READ_MULTIPLIER * (steps - 1)
        + total_variable
    )
    without_cache = stable * steps + total_variable
    return {
        "measurable": True,
        "stable_identical": True,
        "steps": steps,
        "cacheable_tokens": stable,
        "billable_with_cache": round(with_cache),
        "billable_without_cache": round(without_cache),
        "savings_ratio": round(1 - with_cache / without_cache, 4) if without_cache else 0.0,
    }


def build_preview(
    skill,
    project,
    *,
    execution: SkillExecution | None = None,
    positions: list[int] | None = None,
    measure_fragments: bool = False,
) -> WorkflowPreview:
    """
    El presupuesto de contexto de cada paso, sin correr el workflow.

    ``measure_fragments`` decide si se buscan fragmentos dentro de los
    documentos que no entran completos. Apagado por defecto porque cuesta una
    búsqueda por paso y sólo cambia la parte variable; cuando está apagado,
    ``variable_tokens`` viaja como ``None`` y no como cero.
    """
    # Import local: `services` importa este módulo indirectamente a través del
    # comando, y a nivel de módulo el ciclo se cierra.
    from apps.skill.services import (
        _resolve_step_documents,
        _with_operation_context,
        build_step_corpus,
        resolve_documents,
        resolve_tier,
        _resolve_model,
    )

    steps: list[SkillStep] = list(skill.steps.all())
    if not steps:
        raise ValueError("El workflow no tiene pasos definidos.")

    execution = execution or simulated_execution(skill, project)
    documents = resolve_documents(execution)
    blueprint_id = getattr(project, "blueprint_document_id", None)
    blueprint = getattr(project, "blueprint_document", None)

    system_prompt = _with_operation_context(skill.system_prompt, execution)
    system_tokens = context_budget.estimate_tokens(system_prompt)
    output_reserve = context_budget.output_reserve()

    document_texts: dict[int, str] = {}
    previews: list[StepPreview] = []
    cacheable: list[int] = []
    variable: list[int] = []
    warnings: list[str] = []

    for step in steps:
        if positions and step.position not in positions:
            continue

        tier = resolve_tier(skill, step)
        step_tokens = context_budget.estimate_tokens(
            f"{step.title}\n{step.instructions}"
        )
        # La reserva del paso sin las secciones previas: acá no hay corrida, así
        # que no existen. Es el mejor caso; en la corrida real el historial come
        # presupuesto y algún documento más puede degradarse.
        reserved = system_tokens + step_tokens + output_reserve

        evidence_mode = step.evidence_mode or StepEvidenceMode.BOTH
        reads_documents = evidence_mode != StepEvidenceMode.PREVIOUS

        if not reads_documents:
            # Un paso que integra resultados anteriores no recibe documentos:
            # el runner ni siquiera arma el corpus. Contarle una base documental
            # acá inflaría su presupuesto y mostraría en el panel una evidencia
            # que en la corrida no va a existir.
            previews.append(
                StepPreview(
                    position=step.position,
                    title=step.title,
                    tier=tier,
                    tier_source="step" if step.tier else "skill",
                    model=_resolve_model(skill, tier),
                    evidence_mode=evidence_mode,
                    reads_documents=False,
                    reserved_tokens=reserved,
                    cacheable_tokens=0,
                    variable_tokens=0,
                    total_tokens=reserved,
                    exceeds_window=reserved > context_budget.CONTEXT_WINDOW,
                    documents=[],
                )
            )
            cacheable.append(0)
            variable.append(reserved)
            continue

        step_documents = _resolve_step_documents(step, documents, [])
        corpus = build_step_corpus(
            execution=execution,
            step_documents=step_documents,
            query_text=f"{step.title}. {step.instructions}".strip(),
            reserved_tokens=reserved,
            blueprint_id=blueprint_id,
            document_texts=document_texts,
            retrieve_partials=measure_fragments,
        )
        plan = corpus.plan
        stable = "\n\n".join([corpus.inventory] + [d.text for d in corpus.documents])
        stable_tokens = context_budget.estimate_tokens(stable)
        volatile_tokens = (
            context_budget.estimate_tokens(corpus.volatile) if measure_fragments else None
        )
        total = reserved + stable_tokens + (volatile_tokens or 0)

        docs = [
            DocumentPreview(
                slug=d.slug,
                name=getattr(d.document, "name", d.slug),
                mode=d.mode,
                tokens=d.tokens,
                full_tokens=d.full_tokens,
                is_blueprint=d.is_blueprint,
                reason=d.reason,
            )
            for d in plan.deliveries
        ]

        previews.append(
            StepPreview(
                position=step.position,
                title=step.title,
                tier=tier,
                tier_source="step" if step.tier else "skill",
                model=_resolve_model(skill, tier),
                evidence_mode=evidence_mode,
                reads_documents=True,
                reserved_tokens=reserved,
                cacheable_tokens=stable_tokens,
                variable_tokens=volatile_tokens,
                total_tokens=total,
                exceeds_window=total > context_budget.CONTEXT_WINDOW,
                documents=docs,
            )
        )
        cacheable.append(stable_tokens)
        variable.append(reserved + (volatile_tokens or 0))

        if total > context_budget.CONTEXT_WINDOW:
            warnings.append(
                f"El paso {step.position} excede la ventana: {total:,} tokens."
            )
        degraded_blueprint = plan.blueprint
        if degraded_blueprint is not None and degraded_blueprint.mode != context_budget.FULL:
            warnings.append(
                f"El paso {step.position} degrada el documento principal "
                f"[{degraded_blueprint.slug}]: el informe se escribiría sin el "
                "texto completo de la operación que evalúa."
            )

    return WorkflowPreview(
        skill={
            "slug": skill.slug,
            "name": skill.name,
            "tier": skill.tier,
            "steps_total": len(steps),
        },
        project={
            "slug": project.slug,
            "name": project.name,
            "documents": documents.count(),
            "blueprint": (
                {"slug": blueprint.slug, "name": blueprint.name} if blueprint else None
            ),
        },
        window={
            "context_window": context_budget.CONTEXT_WINDOW,
            "safety_margin": context_budget.CONTEXT_SAFETY_MARGIN,
            "output_reserve": output_reserve,
            "system_tokens": system_tokens,
            "chars_per_token": context_budget.CHARS_PER_TOKEN,
        },
        steps=previews,
        cache=_cache_report(cacheable, variable),
        warnings=warnings,
        fragments_measured=measure_fragments,
    )
