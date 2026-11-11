import django.db.models.deletion
from django.db import migrations, models


def borrar_fichas_anteriores(apps, schema_editor):
    """Las fichas armadas antes de este cambio salían del conocimiento general
    del modelo, no de una NDC. Se borran: se vuelven a armar, desde la
    biblioteca, la próxima vez que se analice un mes."""
    apps.get_model("proyeccion", "FichaNDC").objects.all().delete()


class Migration(migrations.Migration):
    """La ficha NDC recuerda de qué NDC de la biblioteca pública salió."""

    dependencies = [
        ("document", "0027_mision_de_evaluacion_caf"),
        ("proyeccion", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="fichandc",
            name="documento",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="document.document",
            ),
        ),
        migrations.AddField(
            model_name="fichandc",
            name="documento_nombre",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="fichandc",
            name="documento_anio",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.RunPython(borrar_fichas_anteriores, migrations.RunPython.noop),
    ]
