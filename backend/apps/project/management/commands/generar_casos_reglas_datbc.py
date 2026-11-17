"""Regenera la tabla de casos de las reglas de la DATBC (ver `casos_reglas_datbc`).

    python manage.py generar_casos_reglas_datbc [--salida RUTA]

Después hay que copiar el archivo a `ecofiliaV2/tests/fixtures/caf/reglas-datbc.json`.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.project.services.casos_reglas_datbc import casos

DESTINO = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "reglas_datbc.json"


class Command(BaseCommand):
    help = "Regenera la tabla de casos de las reglas de la DATBC compartida con el portal."

    def add_arguments(self, parser):
        parser.add_argument("--salida", default=str(DESTINO))

    def handle(self, *args, **options):
        salida = Path(options["salida"])
        salida.write_text(json.dumps(casos(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        self.stdout.write(f"Escrito {salida}. Copialo a ecofiliaV2/tests/fixtures/caf/reglas-datbc.json.")
