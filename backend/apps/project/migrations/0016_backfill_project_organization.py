"""
A qué institución pertenece cada operación que ya existe.

Hasta acá se deducía en cada consulta: una operación era de una institución si
la había creado alguien de ella o si tenía habilitado alguno de sus
asistentes. El segundo criterio deja de servir en cuanto dos instituciones
comparten un asistente —una operación de una aparecería en el portal de la
otra—, así que la pertenencia pasa a ser un dato.

Se completa con ese mismo criterio, en orden:

1. La organización del dueño, si tiene.
2. Si la armó alguien sin organización (el equipo de Ecofilia), la única
   organización que tiene habilitado alguno de sus asistentes.

Si los asistentes apuntan a más de una organización, la operación queda sin
asignar y se informa: se decide a mano desde el admin.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Project = apps.get_model("project", "Project")
    Organization = apps.get_model("user", "Organization")

    ambiguous = []
    for project in Project.objects.filter(organization__isnull=True).select_related("owner"):
        org_id = project.owner.organization_id
        if org_id is None:
            candidates = list(
                Organization.objects.filter(enabled_skills__in=project.enabled_skills.all())
                .values_list("id", flat=True)
                .distinct()
            )
            if len(candidates) == 1:
                org_id = candidates[0]
            elif len(candidates) > 1:
                ambiguous.append(project.slug)
        if org_id is not None:
            Project.objects.filter(pk=project.pk).update(organization_id=org_id)

    if ambiguous:
        print(
            "\n  Operaciones con asistentes de más de una organización, sin asignar: "
            + ", ".join(ambiguous)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("project", "0015_project_organization"),
        ("skill", "0031_skill_members_can_run"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
