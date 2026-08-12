from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("document", "0012_smartchunk_page_number_dedup_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="page_count",
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text="Páginas del archivo original (solo PDF). Null si no aplica o no se pudo leer.",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="pages_with_text",
            field=models.IntegerField(
                blank=True,
                null=True,
                help_text="Páginas de las que se extrajo texto utilizable. Comparar con page_count da la cobertura real.",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="parser_used",
            field=models.CharField(
                blank=True,
                max_length=20,
                help_text="Extractor que produjo el texto (pymupdf, pypdf2, docx, txt).",
            ),
        ),
        migrations.AlterField(
            model_name="document",
            name="chunking_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("processing", "Processing"),
                    ("done", "Done"),
                    ("partial", "Partial"),
                    ("error", "Error"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
