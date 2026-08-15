"""
Modo de evidencia por paso.

No todos los pasos de un informe leen documentos. Los que integran resultados
—"indicá la determinación a partir de los criterios A1, A2 y A3", "tomá las
determinaciones de los pasos anteriores sin volver a analizarlas"— trabajan
sobre lo ya redactado. Hasta ahora igual disparaban una recuperación y recibían
fragmentos que no debían usar: además de gastar presupuesto, es una de las vías
por las que una sección termina citando documentos ajenos a su alcance.

El default es `both`, que es el comportamiento actual: esta migración no cambia
ninguna corrida por sí sola. Marcar los pasos que sintetizan es una decisión de
autoría del workflow, no algo que corresponda inferir acá.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0019_skill_tier"),
    ]

    operations = [
        migrations.AddField(
            model_name="skillstep",
            name="evidence_mode",
            field=models.CharField(
                choices=[
                    ("documents", "Solo documentos"),
                    ("previous_steps", "Solo pasos previos"),
                    ("both", "Documentos y pasos previos"),
                ],
                default="both",
                help_text=(
                    "Con qué material trabaja el paso. Los pasos que integran resultados "
                    "anteriores no deberían recibir documentos: no los necesitan y es una "
                    "vía por la que terminan citando fuentes ajenas a su sección."
                ),
                max_length=20,
            ),
        ),
    ]
