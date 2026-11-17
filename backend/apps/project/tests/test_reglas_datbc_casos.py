"""
La tabla de casos de las reglas de la DATBC está al día con las reglas.

El portal prueba su copia de las reglas contra el mismo archivo
(`ecofiliaV2/tests/fixtures/caf/reglas-datbc.json`). Si este test falla, las
reglas cambiaron: regenerar con `python manage.py generar_casos_reglas_datbc`,
copiar el archivo al front y ajustar `lib/caf-alineacion.ts` hasta que su test
pase.
"""
import json
from pathlib import Path

from django.test import SimpleTestCase

from apps.project.services.casos_reglas_datbc import casos

ARCHIVO = Path(__file__).resolve().parent / "fixtures" / "reglas_datbc.json"


class CasosReglasDatbcTestCase(SimpleTestCase):
    def test_the_shared_case_table_matches_the_rules(self):
        guardado = json.loads(ARCHIVO.read_text(encoding="utf-8"))
        self.assertEqual(guardado, casos())

    def test_the_table_covers_every_combination(self):
        guardado = json.loads(ARCHIVO.read_text(encoding="utf-8"))
        self.assertEqual(len(guardado["objetivo_1"]), 7 * 5 * 4)
        self.assertEqual(len(guardado["objetivo_2"]), 3 * 3 * 4 * 4 * 3)
