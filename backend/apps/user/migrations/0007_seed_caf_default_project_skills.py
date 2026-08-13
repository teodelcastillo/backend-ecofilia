"""
Attach the IET workflow to CAF as the default agent of every operation.

Three things have to be true for a CAF ejecutivo to run the IET workflow on
an operation, and none of them held before this migration:

1. the skill is visible to the org  -> Organization.enabled_skills
2. new operations start with it     -> Organization.default_project_skills
3. operations created BEFORE (2)    -> backfilled here

(3) is deliberately conservative: it only touches CAF projects whose skill
list is empty, which are unusable as they stand — the portal gives ejecutivos
no UI to attach an agent, so an empty workspace is a dead end. A project that
already has skills reflects a deliberate choice made from the admin and is
left alone.
"""
from django.db import migrations

CAF_SLUG = "caf"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"


def seed_caf_defaults(apps, schema_editor):
    Organization = apps.get_model("user", "Organization")
    Skill = apps.get_model("skill", "Skill")
    Project = apps.get_model("project", "Project")
    db_alias = schema_editor.connection.alias

    org = Organization.objects.using(db_alias).filter(slug=CAF_SLUG).first()
    skill = Skill.objects.using(db_alias).filter(slug=IET_SLUG).first()
    if org is None or skill is None:
        # Fresh database or a deployment without the CAF org: nothing to seed.
        return

    # M2M .add() is idempotent, so re-running is harmless.
    org.enabled_skills.add(skill)
    org.default_project_skills.add(skill)

    orphan_projects = (
        Project.objects.using(db_alias)
        .filter(owner__organization=org, enabled_skills__isnull=True)
        .distinct()
    )
    for project in orphan_projects:
        project.enabled_skills.add(skill)


def unseed_caf_defaults(apps, schema_editor):
    """Undo the org-level defaults only.

    Projects are left as they are: by the time anyone reverses this, the skill
    lists may carry assignments made from the admin, and there is no way to
    tell those apart from the ones this migration wrote.
    """
    Organization = apps.get_model("user", "Organization")
    Skill = apps.get_model("skill", "Skill")
    db_alias = schema_editor.connection.alias

    org = Organization.objects.using(db_alias).filter(slug=CAF_SLUG).first()
    skill = Skill.objects.using(db_alias).filter(slug=IET_SLUG).first()
    if org is None or skill is None:
        return
    org.default_project_skills.remove(skill)


class Migration(migrations.Migration):

    dependencies = [
        ("project", "0008_project_deliverable"),
        ("user", "0006_organization_default_project_skills"),
    ]

    operations = [
        migrations.RunPython(seed_caf_defaults, unseed_caf_defaults),
    ]
