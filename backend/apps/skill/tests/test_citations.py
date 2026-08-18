"""
El circuito de trazabilidad: de la cita de la API a la página verificable.

Estos tests cubren lo que el producto promete y hasta ahora no podía cumplir:
que una afirmación del informe se pueda ir a verificar al documento. La cadena
tiene tres eslabones y cada uno puede romperse en silencio —una cita atribuida
al documento equivocado, una página estimada de más, un texto citado que no
está donde dice— así que los tres se prueban por separado.
"""
from dataclasses import dataclass

from django.test import SimpleTestCase

from apps.document.page_map import build_page_map, format_reference
from apps.skill.citations import citation_stats, resolve_citations


@dataclass
class FakePayload:
    """Lo mínimo que `resolve_citations` necesita de un documento enviado."""

    slug: str
    name: str
    text: str
    page_map: object


def payload(slug: str, pages: list[str]) -> FakePayload:
    raw = "\n\n".join(f"<<<PAGE:{i}>>>\n\n{t}" for i, t in enumerate(pages, start=1))
    page_map = build_page_map(raw)
    return FakePayload(slug=slug, name=slug.upper(), text=page_map.text, page_map=page_map)


def citation(payload_obj: FakePayload, phrase: str, *, index: int = 0, **overrides) -> dict:
    """Una cita como la devuelve la API sobre una fuente de texto plano."""
    start = payload_obj.text.index(phrase)
    base = {
        "type": "char_location",
        "cited_text": phrase,
        "document_index": index,
        "document_title": f"[{payload_obj.slug}]",
        "start_char_index": start,
        "end_char_index": start + len(phrase),
        "start_page_number": None,
        "end_page_number": None,
    }
    base.update(overrides)
    return base


class PageMapTests(SimpleTestCase):
    def test_marker_positions_become_page_boundaries(self):
        page_map = build_page_map("<<<PAGE:1>>>\n\nUno.\n\n<<<PAGE:2>>>\n\nDos.")

        self.assertEqual(page_map.text, "Uno.\n\nDos.")
        self.assertEqual(page_map.page_at(page_map.text.index("Uno")), 1)
        self.assertEqual(page_map.page_at(page_map.text.index("Dos")), 2)

    def test_numbering_need_not_start_at_one(self):
        """Las portadas sin texto no producen marcador, así que un documento
        puede empezar en la página 3."""
        page_map = build_page_map("<<<PAGE:3>>>\nTres.\n<<<PAGE:530>>>\nQuinientos.")

        self.assertEqual(page_map.page_at(0), 3)
        self.assertEqual(page_map.page_at(page_map.text.index("Quinientos")), 530)

    def test_document_without_markers_yields_no_page(self):
        """Los documentos del método viejo no tienen marcadores. Se declara la
        ausencia; estimar una página sería peor que no dar ninguna."""
        page_map = build_page_map("Texto plano, sin marcadores.")

        self.assertFalse(page_map.has_pages)
        self.assertIsNone(page_map.page_at(5))
        self.assertEqual(format_reference(None, None), "sin página")

    def test_range_spanning_two_pages(self):
        page_map = build_page_map("<<<PAGE:7>>>\nUno.\n<<<PAGE:8>>>\nDos.")
        start = page_map.text.index("Uno")
        end = page_map.text.index("Dos") + 3

        self.assertEqual(page_map.page_range(start, end), (7, 8))
        self.assertEqual(format_reference(7, 8), "pp. 7–8")


class ResolveCitationsTests(SimpleTestCase):
    def setUp(self):
        self.nap = payload("nap", ["Portada.", "El NAP define metas al 2030.", "Anexo."])
        self.payloads = [self.nap]

    def test_citation_resolves_to_document_and_page(self):
        resolved = resolve_citations(
            [citation(self.nap, "El NAP define metas al 2030.")], self.payloads
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["document_slug"], "nap")
        self.assertEqual(resolved[0]["page_start"], 2)
        self.assertEqual(resolved[0]["reference"], "p. 2")
        self.assertTrue(resolved[0]["verified"])

    def test_text_that_is_not_where_it_claims_fails_verification(self):
        """El punto entero de verificar: la API devuelve el texto literal, así
        que se puede comprobar en vez de creerle."""
        bad = citation(self.nap, "El NAP define metas al 2030.")
        bad["cited_text"] = "El NAP prohíbe las metas al 2030."

        resolved = resolve_citations([bad], self.payloads)

        self.assertFalse(resolved[0]["verified"])
        self.assertEqual(len(resolved), 1, "una cita falsa se registra, no se oculta")

    def test_whitespace_differences_do_not_fail_verification(self):
        """Un salto de línea de más en el recorte no es una cita falsa, y
        contarlo como tal haría inservible la métrica."""
        reformatted = citation(self.nap, "El NAP define metas al 2030.")
        reformatted["cited_text"] = "El NAP  define\nmetas al 2030."

        self.assertTrue(resolve_citations([reformatted], self.payloads)[0]["verified"])

    def test_out_of_range_document_index_is_dropped(self):
        """Caer al primer documento atribuiría la cita al equivocado, que es
        exactamente el defecto que esta fase viene a cerrar."""
        for index in (99, -1, None):
            with self.subTest(index=index):
                bad = citation(self.nap, "Anexo.", index=0)
                bad["document_index"] = index
                self.assertEqual(resolve_citations([bad], self.payloads), [])

    def test_pdf_page_location_is_used_when_present(self):
        """Si algún día la fuente es un PDF, la API ya trae la página y no hay
        que deducirla del offset."""
        from_pdf = citation(
            self.nap, "Anexo.", start_page_number=41, end_page_number=42
        )

        resolved = resolve_citations([from_pdf], self.payloads)

        self.assertEqual((resolved[0]["page_start"], resolved[0]["page_end"]), (41, 42))

    def test_document_without_markers_cites_without_page(self):
        plain = FakePayload(
            slug="ndc", name="NDC",
            text="Texto plano de la NDC.", page_map=build_page_map("Texto plano de la NDC."),
        )

        resolved = resolve_citations([citation(plain, "plano de la NDC")], [plain])

        self.assertTrue(resolved[0]["verified"])
        self.assertIsNone(resolved[0]["page_start"])
        self.assertEqual(resolved[0]["reference"], "sin página")


class CitationStatsTests(SimpleTestCase):
    def test_counts_verified_and_locatable_separately(self):
        """Una cita cierta pero no ubicable no es lo mismo que una que manda a
        la página 47, y el informe no debería presentarlas igual."""
        resolved = [
            {"verified": True, "page_start": 2, "document_slug": "nap"},
            {"verified": True, "page_start": None, "document_slug": "ndc"},
            {"verified": False, "page_start": 5, "document_slug": "nap"},
        ]

        stats = citation_stats(resolved)

        self.assertEqual(stats["citations"], 3)
        self.assertEqual(stats["citations_verified"], 2)
        self.assertEqual(stats["citations_with_page"], 2)
        self.assertEqual(stats["citations_documents"], ["nap", "ndc"])

    def test_no_citations_reports_zero_rather_than_nothing(self):
        """Un paso sin citas tiene que quedar distinguible de un paso que no
        registró citas — si no, no se puede auditar la corrida."""
        self.assertEqual(citation_stats([]), {"citations": 0})
