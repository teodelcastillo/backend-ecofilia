"""
El alcance de los asistentes que ya existen (fase 0: aislamiento).

Hasta acá un asistente sin dueño lo veía cualquiera. Desde ``0048`` decide
``Skill.scope``, y esta migración lo fija para lo que ya hay:

1. ``caf-*`` → de la institución CAF. Si la base no tiene la institución CAF
   (una base nueva, un entorno de prueba), quedan en ``platform``: nadie salvo
   el superadmin las ve, y la migración no falla.
2. Con dueño → ``user``: las sigue viendo sólo su dueño.
3. El resto, sin dueño → ``platform``. No hay ninguna marca de «público»: el
   dueño del producto confirma después cuáles se abren
   (``python manage.py listar_skills_alcance --hacer-publicas ...``).

Las versiones de definición de los asistentes de CAF quedan informadas por CAF
(``SkillDefinitionVersion.informed_by_organization``).

Reversible: al volver, todo queda en ``platform`` sin institución, que es lo
que ``0048`` deja al revertirse de todos modos.
"""
from django.db import migrations

CAF_SLUG = "caf"
CAF_PREFIX = "caf-"


def asignar(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Organization = apps.get_model("user", "Organization")
    SkillDefinitionVersion = apps.get_model("skill", "SkillDefinitionVersion")
    db = schema_editor.connection.alias if schema_editor else "default"

    skills = Skill.objects.using(db)
    caf = Organization.objects.using(db).filter(slug=CAF_SLUG).first()
    de_caf = skills.filter(slug__startswith=CAF_PREFIX)
    resto = skills.exclude(slug__startswith=CAF_PREFIX)

    if caf is not None:
        de_caf.update(scope="organization", organization=caf)
        SkillDefinitionVersion.objects.using(db).filter(
            skill__slug__startswith=CAF_PREFIX
        ).update(informed_by_organization=caf)
    else:
        de_caf.update(scope="platform", organization=None)
    resto.filter(owner__isnull=False).update(scope="user", organization=None)
    resto.filter(owner__isnull=True).update(scope="platform", organization=None)


def revertir(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillDefinitionVersion = apps.get_model("skill", "SkillDefinitionVersion")
    db = schema_editor.connection.alias if schema_editor else "default"

    Skill.objects.using(db).update(scope="platform", organization=None)
    SkillDefinitionVersion.objects.using(db).update(informed_by_organization=None)


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0048_skill_scope_y_organizacion"),
        ("user", "0007_seed_caf_default_project_skills"),
    ]

    operations = [
        migrations.RunPython(asignar, revertir),
    ]
