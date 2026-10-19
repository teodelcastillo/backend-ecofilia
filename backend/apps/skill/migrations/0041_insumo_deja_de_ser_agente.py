"""
El insumo de Ecofilia deja de ser un agente.

Desde ``0040`` el insumo es la ficha que el portal arma a partir de la IET: la
etapa IDO ya no lo lanza (ver ``apps.project.services.stages``) y nadie tiene
que poder correrlo a mano. El workflow y sus corridas quedan como historial de
las operaciones que lo tuvieron; lo que se retira es su presencia: sale de los
agentes habilitados de cada operación y de los que la organización CAF da por
defecto a una operación nueva, y ``members_can_run`` queda en falso.
"""
from django.db import migrations

INSUMO_SLUG = "caf-insumo-ejecutivo"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
CAF_ORG_SLUG = "caf"


def retirar(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Project = apps.get_model("project", "Project")
    Organization = apps.get_model("user", "Organization")

    insumo = Skill.objects.filter(slug=INSUMO_SLUG).first()
    if insumo is None:
        return
    if insumo.members_can_run:
        insumo.members_can_run = False
        insumo.save(update_fields=["members_can_run"])
    for project in Project.objects.filter(enabled_skills=insumo):
        project.enabled_skills.remove(insumo)
    for org in Organization.objects.filter(default_project_skills=insumo):
        org.default_project_skills.remove(insumo)


def reponer(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Project = apps.get_model("project", "Project")
    Organization = apps.get_model("user", "Organization")

    insumo = Skill.objects.filter(slug=INSUMO_SLUG).first()
    if insumo is None:
        return
    org = Organization.objects.filter(slug=CAF_ORG_SLUG).first()
    if org is not None:
        org.default_project_skills.add(insumo)
    iet = Skill.objects.filter(slug=IET_SLUG).first()
    if iet is not None:
        for project in Project.objects.filter(enabled_skills=iet):
            project.enabled_skills.add(insumo)


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0040_iet_sin_insumo_y_componentes_verdes"),
        ("project", "0017_stage_transitions_and_reports"),
        ("user", "0007_seed_caf_default_project_skills"),
    ]

    operations = [
        migrations.RunPython(retirar, reponer),
    ]
