from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0029_skill_model_blank_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="skillstep",
            name="history_mode",
            field=models.CharField(
                choices=[("auto", "Automático"), ("selected", "Elegidas")],
                default="auto",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="history_positions",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Con history_mode='selected', las posiciones de los pasos previos que ve.",
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="reasoning_effort",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Por defecto"),
                    ("low", "Bajo"),
                    ("medium", "Medio"),
                    ("high", "Alto"),
                    ("xhigh", "Muy alto"),
                    ("max", "Máximo"),
                ],
                default="",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="reasoning_instructions",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="skill",
            name="default_format_instructions",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Formato por defecto de la respuesta de cada paso de texto. Un paso "
                    "con formato propio lo reemplaza; vacío, rige el formato profesional "
                    "del motor."
                ),
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="comparative_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Con el modo comparativo del workflow activado, si este paso recibe "
                    "sus reglas. Apagado, el paso no organiza su respuesta por documento."
                ),
            ),
        ),
        migrations.AddField(
            model_name="skillstep",
            name="format_instructions",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Formato de la respuesta del paso. Si se completa, reemplaza al "
                    "formato profesional por defecto y a las reglas de formato del modo "
                    "comparativo."
                ),
            ),
        ),
    ]
