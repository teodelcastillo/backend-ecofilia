from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0016_seed_evidence_tags"),
        ("project", "0008_project_deliverable"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectdocument",
            name="tags",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Qué papel cumple este documento en esta operación. Es lo que "
                    "consultan los pasos que piden su evidencia por etiqueta. Se "
                    "pre-carga desde los topics de la biblioteca al vincular, y desde "
                    "ahí manda lo que decida el equipo de la operación. Vacío es un "
                    "estado válido: el documento sigue entrando en los pasos que leen "
                    "todo el expediente y en los que lo nombran explícitamente."
                ),
                related_name="project_documents",
                to="document.evidencetag",
            ),
        ),
    ]
