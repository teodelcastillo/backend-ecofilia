import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0023_reliability_stalled_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="skillstep",
            name="evidence_selection",
            field=models.CharField(
                choices=[
                    ("all", "Todo el expediente de la operación"),
                    ("tagged", "Documentos con las etiquetas indicadas"),
                    ("blueprint_only", "Sólo el documento principal"),
                    ("manual", "Sólo los documentos nombrados"),
                ],
                default="all",
                help_text=(
                    "Cómo elige este paso su base documental. El default lee todo el "
                    "expediente, que es lo que hacían todos los pasos antes de que "
                    "existieran las etiquetas."
                ),
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="evidence_tags",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.SlugField(max_length=80),
                blank=True,
                default=list,
                help_text=(
                    "Etiquetas cuyos documentos lee este paso, cuando "
                    "evidence_selection='tagged'. Se guardan como slugs y no como "
                    "claves foráneas porque la definición del workflow se serializa "
                    "por valor: una corrida vieja tiene que poder decir qué pidió "
                    "aunque después se renombre o se borre la etiqueta."
                ),
                size=None,
            ),
        ),
        migrations.AlterField(
            model_name="skillstep",
            name="document_slugs",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Documentos nombrados explícitamente. Con evidence_selection="
                    "'manual' son el conjunto del paso; con 'tagged' se suman a los "
                    "que aportan las etiquetas."
                ),
            ),
        ),
    ]
