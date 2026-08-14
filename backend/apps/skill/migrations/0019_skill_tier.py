"""
Tier por skill y por paso, en lugar de un id de modelo guardado en la base.

Un id concreto envejece en silencio: el workflow del IET arrastraba
`gpt-4o-mini` desde su propia migración de alta, y como `effective_chat_model`
sólo respeta el valor guardado cuando es un id de Claude explícito, nadie podía
notar desde la aplicación en qué modelo estaba corriendo realmente. El tier
describe la capacidad que el paso necesita y deja que `LLM_MODEL_FAST` /
`_BALANCED` / `_DEEP` resuelvan el modelo en tiempo de request, de modo que
adoptar una generación nueva sea cambiar una variable de entorno.

**Cambio de comportamiento deliberado.** Hasta acá los copilots resolvían
siempre al tier profundo por ser copilots. Con esta migración pasan al
equilibrado, que es el adecuado para el perfil real de estos workflows:
redacción muy instruida sobre evidencia acotada, donde importa la precisión y
el cumplimiento del prompt más que la profundidad de razonamiento. Los pasos que
sí piden juicio —integrar criterios técnicos en una determinación— se marcan
individualmente como profundos desde el builder.

`Skill.model` se conserva como escotilla de escape: un id de Claude explícito
sigue teniendo precedencia sobre el tier.
"""
from django.db import migrations, models

TIER_CHOICES = [
    ("fast", "Rápido"),
    ("balanced", "Equilibrado"),
    ("deep", "Profundo"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0018_seed_caf_iet_datbc_workflow"),
    ]

    operations = [
        migrations.AddField(
            model_name="skill",
            name="tier",
            field=models.CharField(
                choices=TIER_CHOICES,
                default="balanced",
                help_text=(
                    "Capacidad por defecto de la skill. Cada paso puede pedir otra. "
                    "El equilibrado alcanza para redacción muy instruida sobre "
                    "evidencia acotada; el profundo es para síntesis y juicio."
                ),
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="tier",
            field=models.CharField(
                blank=True,
                choices=TIER_CHOICES,
                default="",
                help_text=(
                    "Capacidad para este paso. Vacío = la del workflow. Los pasos de un "
                    "mismo informe no son homogéneos: describir un marco de políticas a "
                    "partir de documentos no pide lo mismo que integrar criterios en una "
                    "determinación."
                ),
                max_length=20,
            ),
        ),
    ]
