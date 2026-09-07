import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0014_smartchunk_chunk_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="EvidenceTag",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("slug", models.SlugField(max_length=80, unique=True)),
                ("name", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True)),
                (
                    "source_topics",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.TextField(),
                        blank=True,
                        default=list,
                        help_text=(
                            "Topics de la biblioteca que pre-marcan esta etiqueta al "
                            "vincular un documento a una operación. En minúsculas."
                        ),
                        size=None,
                    ),
                ),
                (
                    "is_seed",
                    models.BooleanField(
                        default=False,
                        help_text="Etiqueta provista por Ecofilia. No se borra desde la API.",
                    ),
                ),
                ("position", models.PositiveIntegerField(default=100)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("position", "name"),
            },
        ),
    ]
