"""
Alta del workflow que redacta el informe del ejecutivo con su criterio.

Es la vía "guiada": el ejecutivo completa en la pestaña Alineación su
determinación y su fundamento para cada criterio —con el insumo de Ecofilia como
referencia— y este workflow redacta el IET con ese criterio. El resultado se
trabaja después en el Editor, como cualquier informe.

Se arma copiando, al correr la migración, la configuración y los pasos de la
IET de producción (prompts, modelo, evidencia por paso), que ya están afinados.
Así el informe guiado lee los mismos documentos y escribe con la misma
estructura: los títulos son los mismos, y el portal lo arma con el mismo visor.
Lo único que cambia es que los pasos D y E reciben el criterio del ejecutivo
—por el parámetro ``criterio_ejecutivo``, que el motor agrega a cada paso— y no lo discuten: redactan el
análisis que lo sustenta y dejan como observación lo que la evidencia no
alcance a sostener.

La copia no se mantiene sincronizada: si se afina la IET, el guiado se ajusta
aparte desde el Studio. El paso de síntesis de la IET ("S.") no se copia: la
mirada estructurada del ejecutivo es la que él mismo cargó.
"""
from django.db import migrations

SLUG = "caf-iet-guiado-ejecutivo"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
CAF_ORG_SLUG = "caf"
PARAM_KEY = "criterio_ejecutivo"

SYSTEM_PROMPT_SUFFIX = (
    "\n\nEn esta corrida redactás la versión del ejecutivo del informe. Sus "
    "determinaciones y fundamentos, criterio por criterio, te llegan en los pasos "
    "que los necesitan: son los que valen. Tu trabajo es redactar el análisis que "
    "los sustenta con la evidencia de los documentos, no llegar a una conclusión "
    "propia."
)

# Los pasos de un workflow no reemplazan `{{token}}` en sus instrucciones: el
# motor agrega los parámetros de la corrida al prompt de cada paso, bajo
# "Parámetros de la corrida". El bloque remite a esa sección.
CRITERIO_BLOCK = (
    "\n\nEl criterio del ejecutivo para este informe está en los parámetros de la "
    f"corrida («{PARAM_KEY}»): sus determinaciones y fundamentos, criterio por "
    "criterio.\n\n"
    "Este informe es la versión del ejecutivo. La determinación de cada criterio y "
    "su fundamento son los suyos: no los cambies ni los discutas. Redactá el "
    "análisis que los sustenta con la evidencia de los documentos.\n\n"
    "Si la evidencia no alcanza para sostener una determinación, o la contradice, "
    "agregalo al final del paso como «Observación de Ecofilia», en una o dos "
    "oraciones, sin reemplazar la determinación del ejecutivo. Si el ejecutivo no "
    "determinó un criterio de este paso, decilo y no lo determines vos."
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
    "title", "instructions", "format_instructions", "position", "step_type",
    "linked_skill_id", "evidence_selection", "evidence_tags", "document_slugs",
    "tier", "evidence_mode", "output_mode", "approval_required",
    "comparative_enabled", "history_mode", "history_positions",
    "reasoning_effort", "reasoning_instructions", "table_schema",
    "output_validation",
)


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
        name="IET DATBC — Informe guiado por el ejecutivo",
        slug=SLUG,
        description=(
            "Redacta el Informe de Evaluación Técnica con las determinaciones y "
            "fundamentos del ejecutivo, criterio por criterio."
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
            "Determinaciones y fundamentos del ejecutivo por criterio. Lo arma el "
            "portal con lo que el ejecutivo cargó en Alineación."
        ),
        required=True,
        position=1,
    )

    copied = []
    for step in SkillStep.objects.filter(skill=iet).order_by("position"):
        title = (step.title or "").strip()
        if title.startswith("S."):
            continue
        values = {field: getattr(step, field) for field in STEP_FIELDS if hasattr(step, field)}
        if title.startswith(("D.", "E.")):
            values["instructions"] = (values.get("instructions") or "") + CRITERIO_BLOCK
        copied.append(SkillStep(skill=skill, **values))
    SkillStep.objects.bulk_create(copied)

    org = Organization.objects.filter(slug=CAF_ORG_SLUG).first()
    if org is not None:
        # Visible para la institución; no va a las operaciones por defecto:
        # lo lanza el portal cuando el ejecutivo elige la vía guiada.
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
