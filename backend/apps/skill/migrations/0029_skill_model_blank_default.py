"""
`Skill.model` deja de nacer con `gpt-4o-mini`.

El default era `MODEL_COMPLETION`, de cuando todo corría en OpenAI. Bajo
`LLM_PROVIDER=anthropic` el motor descarta cualquier id que no sea de Claude y
resuelve por tier, así que ese valor nunca se usó: sólo hacía que el admin
mostrara un modelo que no es el que corre.

Se limpian también los ya guardados. Sólo se tocan los ids que el motor ignora
(no-Claude); un id de Claude fijado a propósito se respeta. El campo es parte
de la huella de la definición, así que la próxima corrida de cada skill
limpiada registra una versión nueva de su definición, sin cambio de
comportamiento.
"""
from django.db import migrations, models


def clear_ignored_models(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    (
        Skill.objects.exclude(model="")
        .exclude(model__istartswith="claude")
        .exclude(model__istartswith="anthropic.")
        .update(model="")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0028_enable_enverdecimiento_on_caf_operations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="skill",
            name="model",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.RunPython(clear_ignored_models, migrations.RunPython.noop),
    ]
