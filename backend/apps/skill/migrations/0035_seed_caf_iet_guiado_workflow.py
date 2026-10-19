"""
Alta del workflow que reescribe, con el criterio del ejecutivo, las secciones
del IET que cambian.

Es la vía "guiada": el ejecutivo recorre paso a paso los criterios de
alineación y el enverdecimiento —con el insumo de Ecofilia al lado— y este
workflow **no reescribe el informe entero**. Sólo redacta las secciones de
enverdecimiento (C) y alineación (D y E), y en cada paso sólo si el criterio del
ejecutivo difiere del de Ecofilia; si coinciden, responde ``SIN CAMBIOS``. Lo que
escribe se vuelca como borradores sobre la IET de Ecofilia (ver
``apps.project.services.stage_reports``): el ejecutivo los revisa y publica en
el Editor, y el resto del informe base queda como está.

Se arma copiando, al correr la migración, la configuración y los pasos C, D y E
de la IET de producción (prompts, modelo, evidencia por paso), con los mismos
títulos: así cada paso guiado sabe a qué sección de la IET vuelve. Las posiciones
se renumeran y las referencias entre pasos (``history_positions``) se reescriben
para que sigan apuntando a los mismos.

El criterio del ejecutivo, con el de Ecofilia al lado, llega por el parámetro
``criterio_ejecutivo``, que el motor agrega a cada paso. La copia no se mantiene
sincronizada con la IET: si se afina la IET, el guiado se ajusta aparte desde el
Studio. La síntesis de la IET ("S.") no se copia.
"""
from django.db import migrations

SLUG = "caf-iet-guiado-ejecutivo"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
CAF_ORG_SLUG = "caf"
PARAM_KEY = "criterio_ejecutivo"
SIN_CAMBIOS = "SIN CAMBIOS"

SYSTEM_PROMPT_SUFFIX = (
    "\n\nEn esta corrida redactás la versión del ejecutivo de las secciones de "
    "enverdecimiento y alineación del informe. Su criterio, criterio por criterio, "
    "te llega con el de Ecofilia al lado: el del ejecutivo es el que vale. Tu "
    "trabajo es redactar el análisis que lo sustenta con la evidencia de los "
    "documentos, no llegar a una conclusión propia."
)

# Los pasos de un workflow no reemplazan `{{token}}` en sus instrucciones: el
# motor agrega los parámetros de la corrida al prompt de cada paso, bajo
# "Parámetros de la corrida". El bloque remite a esa sección.
CRITERIO_BLOCK = (
    "\n\nEl criterio del ejecutivo para este informe está en los parámetros de la "
    f"corrida («{PARAM_KEY}»): su determinación y fundamento por criterio, su "
    "enverdecimiento, y al lado lo que concluyó Ecofilia.\n\n"
    "Si en lo que trata este paso el ejecutivo coincide con Ecofilia y no aporta un "
    "fundamento distinto, respondé exactamente «" + SIN_CAMBIOS + "» y nada más: la "
    "sección del informe base queda como está.\n\n"
    "Si difiere, redactá la sección completa con su criterio: la determinación y el "
    "fundamento son los del ejecutivo, no los cambies ni los discutas, y sustentalos "
    "con la evidencia de los documentos. Si la evidencia no alcanza o los "
    "contradice, agregalo al final como «Observación de Ecofilia», en una o dos "
    "oraciones, sin reemplazar la determinación del ejecutivo."
)

# Campos de configuración del skill que se heredan de la IET.
SKILL_FIELDS = (
    "system_prompt", "model", "temperature", "tier", "comparative_mode_enabled",
    "strict_missing_evidence", "retrieval_query_template", "retrieval_strategy",
    "k_per_doc", "total_limit", "max_per_doc_after_rerank",
    "default_format_instructions", "pinned_document_slugs",
)
# Campos de cada paso que se copian tal cual.
STEP_FIELDS = (
    "title", "instructions", "format_instructions", "step_type",
    "linked_skill_id", "evidence_selection", "evidence_tags", "document_slugs",
    "tier", "evidence_mode", "output_mode", "approval_required",
    "comparative_enabled", "history_mode", "history_positions",
    "reasoning_effort", "reasoning_instructions", "table_schema",
    "output_validation",
)
COPIED_PREFIXES = ("C.", "D.", "E.")


def create_guiado_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    SkillParameter = apps.get_model("skill", "SkillParameter")
    Organization = apps.get_model("user", "Organization")

    iet = Skill.objects.filter(slug=IET_SLUG).first()
    if iet is None or Skill.objects.filter(slug=SLUG).exists():
        return

    config = {field: getattr(iet, field) for field in SKILL_FIELDS if hasattr(iet, field)}
    config["system_prompt"] = (config.get("system_prompt") or "") + SYSTEM_PROMPT_SUFFIX
    skill = Skill.objects.create(
        owner=None,
        skill_type="copilot",
        name="IET DATBC — Secciones guiadas por el ejecutivo",
        slug=SLUG,
        description=(
            "Reescribe, con el criterio del ejecutivo, las secciones de enverdecimiento "
            "y alineación del IET que difieren de las de Ecofilia."
        ),
        allowed_contexts=["project"],
        prompt_template="",
        members_can_run=False,
        **config,
    )
    SkillParameter.objects.create(
        skill=skill,
        key=PARAM_KEY,
        label="Criterio del ejecutivo",
        param_type="text",
        description=(
            "Determinaciones, fundamentos y enverdecimiento del ejecutivo, con el criterio "
            "de Ecofilia al lado. Lo arma el portal con lo que el ejecutivo completó."
        ),
        required=True,
        position=1,
    )

    source = [
        step
        for step in SkillStep.objects.filter(skill=iet).order_by("position")
        if (step.title or "").strip().startswith(COPIED_PREFIXES)
    ]
    renumber = {step.position: index for index, step in enumerate(source, start=1)}
    copied = []
    for step in source:
        values = {field: getattr(step, field) for field in STEP_FIELDS if hasattr(step, field)}
        values["instructions"] = (values.get("instructions") or "") + CRITERIO_BLOCK
        # Las referencias a pasos que no se copian (los B) se pierden: esos
        # pasos no existen en esta corrida.
        values["history_positions"] = [
            renumber[p] for p in (values.get("history_positions") or []) if p in renumber
        ]
        if values.get("history_mode") == "selected" and not values["history_positions"]:
            values["history_mode"] = "auto"
        copied.append(SkillStep(skill=skill, position=renumber[step.position], **values))
    SkillStep.objects.bulk_create(copied)

    org = Organization.objects.filter(slug=CAF_ORG_SLUG).first()
    if org is not None:
        # Visible para la institución; no va a las operaciones por defecto:
        # lo lanza el portal cuando el ejecutivo genera su informe guiado.
        org.enabled_skills.add(skill)


def remove_guiado_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Skill.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0034_iet_sintesis_por_criterio"),
    ]

    operations = [
        migrations.RunPython(create_guiado_workflow, remove_guiado_workflow),
    ]
