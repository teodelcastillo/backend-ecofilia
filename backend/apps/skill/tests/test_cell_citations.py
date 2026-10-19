"""
Citas dentro de las celdas de una tabla.

En un paso de tabla el modelo no devuelve citas: las escribe dentro de la
celda con su marca interna (``<cite index="doc-oración">…</cite>``). Se
resuelven contra el documento para que la celda tenga su chip con página, y
nunca se muestran crudas.
"""
from django.test import SimpleTestCase

from apps.document.page_map import build_page_map
from apps.skill.citations import resolve_cell_citations, strip_cell_citations
from apps.skill.context_budget import DocumentPayload


def _payload(raw: str, slug="ido", name="Documento de la operación") -> DocumentPayload:
    page_map = build_page_map(raw)
    return DocumentPayload(slug=slug, name=name, text=page_map.text, page_map=page_map)


class CellCitationsTests(SimpleTestCase):
    def setUp(self):
        self.anexo = _payload("Anexo sin relación.", slug="anexo", name="Anexo")
        self.ido = _payload(
            "La operación financia aduanas. La calificación de riesgo climático figura con "
            "las cuatro opciones sin marcar. El prestatario es la República de El Salvador."
        )

    def _table(self, cell):
        return {"type": "table", "columns": ["puntos_clave"], "rows": [{"puntos_clave": cell}]}

    def test_a_literal_quote_becomes_a_verified_chip(self):
        table, citations = resolve_cell_citations(
            self._table('Advertir que es <cite index="1-2">la República de El Salvador</cite>.'),
            [self.anexo, self.ido],
            offset=3,
        )
        self.assertEqual(table["rows"][0]["puntos_clave"], "Advertir que es la República de El Salvador[[c:3]].")
        self.assertEqual(len(citations), 1)
        self.assertEqual((citations[0]["document_slug"], citations[0]["verified"]), ("ido", True))
        self.assertEqual(citations[0]["cited_text"], "la República de El Salvador")

    def test_a_paraphrase_is_located_but_not_verified(self):
        table, citations = resolve_cell_citations(
            self._table('(cite index="1-1">Las opciones de riesgo climático están sin marcar</cite>'),
            [self.anexo, self.ido],
            offset=0,
        )
        self.assertEqual(table["rows"][0]["puntos_clave"], "Las opciones de riesgo climático están sin marcar[[c:0]]")
        self.assertFalse(citations[0]["verified"])
        self.assertIn("cuatro opciones sin marcar", citations[0]["cited_text"])

    def test_an_unknown_document_keeps_only_the_text(self):
        table, citations = resolve_cell_citations(
            self._table('Texto <cite index="9-1">afirmado</cite> y </cite> suelto'), [self.ido], offset=0
        )
        self.assertEqual(table["rows"][0]["puntos_clave"], "Texto afirmado y  suelto")
        self.assertEqual(citations, [])

    def test_strip_for_prompts(self):
        self.assertEqual(
            strip_cell_citations('Ver <cite index="0-1">esto</cite> y aquello[[c:4]].'),
            "Ver esto y aquello.",
        )
