from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0030_skillstep_format_instructions"),
    ]

    operations = [
        migrations.AddField(
            model_name="skill",
            name="members_can_run",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Si los usuarios que no son superadmin pueden ejecutar este asistente "
                    "en las operaciones a las que tienen acceso."
                ),
            ),
        ),
    ]
