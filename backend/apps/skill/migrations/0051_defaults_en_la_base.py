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
        ("skill", "0050_asignar_alcance_skills"),
    ]

    operations = [
        migrations.AlterField(
            model_name="skill",
            name="scope",
            field=models.CharField(
                choices=[
                    ("platform", "Plataforma (sólo superadmin)"),
                    ("public", "Público"),
                    ("organization", "Institución"),
                    ("user", "Usuario"),
                ],
                db_default="platform",
                db_index=True,
                default="platform",
                help_text="Quién ve el asistente: plataforma (sólo superadmin), público, una institución o sólo su dueño.",
                max_length=20,
            ),
        ),
    ]
