from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document", "0013_document_coverage_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="smartchunk",
            name="chunk_type",
            field=models.CharField(
                choices=[("prose", "Prose"), ("table", "Table")],
                default="prose",
                db_index=True,
                max_length=10,
            ),
        ),
    ]
