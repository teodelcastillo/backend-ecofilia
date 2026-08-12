"""Tests de linealización de tablas y su chunking.

La propiedad que importa: cada fila debe poder responder sola. Si una fila
pierde sus encabezados —porque se aplanó, porque el chunk se cortó, o porque se
mezcló con prosa— la tabla deja de ser consultable.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from apps.document.utils.chunker import _semantic_paragraphs
from apps.document.utils.parser import clean_text_spacing
from apps.document.utils.tables import (
    TABLE_END,
    TABLE_START,
    linearize_rows,
    unwrap_table_block,
)

ZONIFICACION_HEADERS = ["Zona", "Altura máxima", "FOS", "Retiro de frente"]
ZONIFICACION_ROWS = [
    ["R1", "12 m", "0,6", "3 m"],
    ["R2", "18 m", "0,7", "2 m"],
    ["C1", "25 m", "0,8", "0 m"],
]


class LinearizeTests(SimpleTestCase):
    def test_each_row_carries_its_headers(self):
        text = linearize_rows(ZONIFICACION_HEADERS, ZONIFICACION_ROWS)
        lines = text.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines[0],
            "R1 — Altura máxima: 12 m; FOS: 0,6; Retiro de frente: 3 m",
        )
        # Ninguna fila depende de otra para interpretarse.
        for line in lines:
            self.assertIn("Altura máxima:", line)
            self.assertIn("FOS:", line)

    def test_empty_cells_are_skipped_not_mislabelled(self):
        rows = [["R1", "12 m", "", "3 m"]]
        line = linearize_rows(ZONIFICACION_HEADERS, rows)
        self.assertIn("Altura máxima: 12 m", line)
        self.assertIn("Retiro de frente: 3 m", line)
        self.assertNotIn("FOS:", line)

    def test_duplicate_and_empty_headers_are_disambiguated(self):
        headers = ["Zona", "Valor", "Valor", ""]
        line = linearize_rows(headers, [["R1", "a", "b", "c"]])
        self.assertIn("Valor: a", line)
        self.assertIn("Valor (2): b", line)
        self.assertIn("col4: c", line)

    def test_falls_back_to_pipes_without_headers(self):
        line = linearize_rows([], [["R1", "12 m", "0,6"]])
        self.assertEqual(line, "R1 | 12 m | 0,6")

    def test_none_cells_do_not_crash(self):
        line = linearize_rows(ZONIFICACION_HEADERS, [["R1", None, "0,6", None]])
        self.assertIn("FOS: 0,6", line)
        self.assertNotIn("None", line)


class TableBlockTests(SimpleTestCase):
    def _block(self) -> str:
        return f"{TABLE_START}\n{linearize_rows(ZONIFICACION_HEADERS, ZONIFICACION_ROWS)}\n{TABLE_END}"

    def test_clean_text_spacing_preserves_row_newlines(self):
        """Colapsar los saltos convertiría la tabla en una tira ilegible."""
        text = f"Un párrafo previo.\n\n{self._block()}\n\nUn párrafo posterior."
        cleaned = clean_text_spacing(text)
        body = unwrap_table_block(
            [p for p in cleaned.split("\n\n") if p.startswith(TABLE_START)][0]
        )
        self.assertEqual(len(body.split("\n")), 3)

    def test_table_becomes_its_own_segment(self):
        text = f"{'Prosa de contexto. ' * 40}\n\n{self._block()}"
        segments = _semantic_paragraphs(text)
        tables = [s for s in segments if s["kind"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertIn("R1 — Altura máxima: 12 m", tables[0]["text"])
        # La prosa no se cuela dentro de la tabla.
        self.assertNotIn("Prosa de contexto", tables[0]["text"])

    def test_long_table_splits_on_row_boundaries(self):
        rows = [[f"Z{i}", f"{i} m", "0,5", "2 m"] for i in range(200)]
        block = f"{TABLE_START}\n{linearize_rows(ZONIFICACION_HEADERS, rows)}\n{TABLE_END}"
        segments = _semantic_paragraphs(block)
        tables = [s for s in segments if s["kind"] == "table"]
        self.assertGreater(len(tables), 1, "una tabla de 200 filas debe partirse")
        for seg in tables:
            for line in seg["text"].split("\n"):
                # Cada trozo sigue siendo interpretable por sí mismo.
                self.assertIn("Altura máxima:", line)
        # Ninguna fila se perdió en el corte.
        emitted = sum(len(s["text"].split("\n")) for s in tables)
        self.assertEqual(emitted, 200)

    def test_short_prose_is_not_folded_into_a_table(self):
        text = f"{self._block()}\n\nNota breve al pie."
        segments = _semantic_paragraphs(text)
        table = [s for s in segments if s["kind"] == "table"][0]
        self.assertNotIn("Nota breve", table["text"])


class HeaderTrustTests(SimpleTestCase):
    """El detector de PyMuPDF asciende filas de datos a encabezado.

    Cada caso de acá salió de un documento real de la biblioteca.
    """

    GLOSARIO = [
        ["IED", "Inversión Extranjera Directa"],
        ["IFC", "Corporación Financiera Internacional"],
    ]

    def test_header_that_appears_as_data_is_rejected(self):
        from apps.document.utils.tables import headers_are_trustworthy

        self.assertFalse(headers_are_trustworthy(
            ["Sigla", "Inversión Extranjera Directa"], 2, self.GLOSARIO))

    def test_real_header_is_accepted(self):
        from apps.document.utils.tables import headers_are_trustworthy

        self.assertTrue(headers_are_trustworthy(
            ["Zona", "Altura máxima", "FOS"], 3,
            [["R1", "12 m", "0,6"], ["R2", "18 m", "0,7"]]))

    def test_sentence_shaped_header_is_rejected(self):
        from apps.document.utils.tables import headers_are_trustworthy

        self.assertFalse(headers_are_trustworthy(
            ["Zona", "Para superficies mayores el Consejo debe determinar."], 2, []))

    def test_single_column_list_is_not_a_table(self):
        from apps.document.utils.tables import looks_like_data_table

        self.assertFalse(looks_like_data_table([["2 m"], ["3 m"], ["4 m"]]))

    def test_prose_region_is_not_a_table(self):
        from apps.document.utils.tables import looks_like_data_table

        self.assertFalse(looks_like_data_table([["a" * 200, "b" * 200]]))

    def test_untrusted_header_falls_back_to_lossless_pipes(self):
        """Sin encabezado confiable la fila queda incompleta, pero nunca falsa."""
        from apps.document.utils.tables import linearize_rows

        first = linearize_rows([], self.GLOSARIO).splitlines()[0]
        self.assertEqual(first, "IED | Inversión Extranjera Directa")
