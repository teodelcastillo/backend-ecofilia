"""
Defaults en la base para las columnas obligatorias que agrega el plan.

El código anterior al plan no conoce estas columnas y, al crear una fila, no las
manda: sin un default en la base, la inserción falla (NOT NULL). Con él, una
vuelta atrás del deploy —volver a la imagen anterior— sigue funcionando sin
revertir migraciones ni perder datos. Ver docs/salida-instituciones.md.

Sólo ``ALTER COLUMN ... SET DEFAULT``: no reescribe la tabla ni toca filas.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0013_institucion_protegida"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="config",
            field=models.JSONField(
                blank=True,
                db_default=models.Value({}, output_field=models.JSONField()),
                default=dict,
            ),
        ),
        migrations.AlterField(
            model_name="user",
            name="failed_login_attempts",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
    ]
