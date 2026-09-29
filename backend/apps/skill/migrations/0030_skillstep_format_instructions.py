from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0029_skill_model_blank_default"),
    ]

    operations = [
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
