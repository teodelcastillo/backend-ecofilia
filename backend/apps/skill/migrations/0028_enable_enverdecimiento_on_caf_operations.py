"""
Habilita el agente de enverdecimiento en las operaciones que ya existen.

El botón «Analizar potencial verde» corre el agente contra la operación, y
``SkillViewSet.run`` exige que esté en los ``enabled_skills`` del proyecto: si
no está, responde 400 y la pestaña queda muerta por más que el agente exista.

La 0027 dio de alta el agente, pero las operaciones creadas antes nacieron con
un único slug habilitado —el del IET, que era lo único que mandaba el
formulario—, así que ninguna puede correrlo.

El criterio para elegir a quién tocar es tener habilitado el agente del IET.
Eso es exactamente lo que define a una operación CAF, y evita encender un
agente de financiamiento verde en proyectos de otras organizaciones que nunca
lo pidieron.

Queda un paso a mano que esta migración no hace a propósito: habilitar el
agente para la organización CAF (``Organization.enabled_skills``). Los
usuarios de una organización restringida sólo ven las skills habilitadas para
ella, así que sin eso el agente sigue invisible aunque el proyecto lo tenga.
"""
from django.db import migrations

ENVERDECIMIENTO_SLUG = "caf-enverdecimiento-operacion"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"


def enable_on_existing_operations(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Project = apps.get_model("project", "Project")

    enverdecimiento = Skill.objects.filter(slug=ENVERDECIMIENTO_SLUG).first()
    iet = Skill.objects.filter(slug=IET_SLUG).first()
    if enverdecimiento is None or iet is None:
        return

    for project in Project.objects.filter(enabled_skills=iet):
        project.enabled_skills.add(enverdecimiento)


def disable_on_existing_operations(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Project = apps.get_model("project", "Project")

    enverdecimiento = Skill.objects.filter(slug=ENVERDECIMIENTO_SLUG).first()
    if enverdecimiento is None:
        return

    for project in Project.objects.filter(enabled_skills=enverdecimiento):
        project.enabled_skills.remove(enverdecimiento)


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0027_seed_caf_enverdecimiento_workflow"),
        ("project", "0010_backfill_project_document_tags"),
    ]

    operations = [
        migrations.RunPython(
            enable_on_existing_operations,
            disable_on_existing_operations,
        ),
    ]
