"""
El otro extremo de la cita: qué frase del informe sostiene.

Una cita tiene dos puntas. ``char_start``/``char_end`` apuntan al documento
fuente —de dónde salió— y eso ya estaba. ``content_start``/``content_end``
apuntan al texto que escribió el modelo —qué afirmación lo usa— y eso se perdía:
se recorrían los bloques de la respuesta quedándose sólo con los arrays de citas
y descartando el ``text`` de cada bloque, que es exactamente el tramo que la
cita sostiene.

No era recuperable después. ``cited_text`` es texto de la fuente, no del
informe, así que no hay nada contra qué machear, y el contenido persistido es la
concatenación de los bloques sin marca alguna. Sin estos offsets una cita sólo
se puede mostrar al pie de la sección; con ellos, junto a lo que sostiene.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.test import SimpleTestCase

from apps.document.page_map import build_page_map
from apps.document.utils.llm import _text_and_citations
from apps.skill.citations import resolve_citations


@dataclass
class FakeCitation:
    cited_text: str
    document_index: int = 0
    type: str = "char_location"
    document_title: str = ""
    start_char_index: int = 0
    end_char_index: int = 10
    start_page_number: object = None
    end_page_number: object = None


@dataclass
class FakeBlock:
    type: str
    text: str = ""
    citations: list = field(default_factory=list)


@dataclass
class FakePayload:
    slug: str
    name: str
    text: str
    page_map: object


def payload(slug: str, pages: list[str]) -> FakePayload:
    raw = "\n\n".join(f"<<<PAGE:{i}>>>\n\n{t}" for i, t in enumerate(pages, start=1))
    page_map = build_page_map(raw)
    return FakePayload(slug=slug, name=slug.upper(), text=page_map.text, page_map=page_map)


class TextAndCitationsTests(SimpleTestCase):
    def test_the_text_is_still_the_concatenation_of_the_blocks(self):
        blocks = [
            FakeBlock("text", "El país adhiere al Acuerdo. "),
            FakeBlock("text", "Su meta es 2030.", [FakeCitation("meta al 2030")]),
        ]
        text, _ = _text_and_citations(blocks)

        self.assertEqual(text, "El país adhiere al Acuerdo. Su meta es 2030.")

    def test_each_citation_points_at_the_span_it_supports(self):
        primero = "El país adhiere al Acuerdo. "
        segundo = "Su meta es 2030."
        blocks = [
            FakeBlock("text", primero),
            FakeBlock("text", segundo, [FakeCitation("meta al 2030")]),
        ]
        text, citations = _text_and_citations(blocks)
        cita = citations[0]

        self.assertEqual(
            text[cita["content_start"]:cita["content_end"]],
            segundo,
            "el span tiene que recortar exactamente la frase que la cita sostiene",
        )

    def test_offsets_survive_the_leading_strip(self):
        """El texto se devuelve con `strip()`. Sin corregir por eso, una cita
        sobre el primer bloque apuntaría unos caracteres más allá."""
        blocks = [
            FakeBlock("text", "\n\n  La meta es 2030.", [FakeCitation("meta 2030")]),
        ]
        text, citations = _text_and_citations(blocks)
        cita = citations[0]

        self.assertEqual(text, "La meta es 2030.")
        self.assertEqual(cita["content_start"], 0)
        self.assertEqual(text[cita["content_start"]:cita["content_end"]], text)

    def test_uncited_blocks_shift_the_following_spans(self):
        blocks = [
            FakeBlock("text", "Introducción sin fuente. "),
            FakeBlock("text", "Afirmación A.", [FakeCitation("a")]),
            FakeBlock("text", " Relleno. "),
            FakeBlock("text", "Afirmación B.", [FakeCitation("b")]),
        ]
        text, citations = _text_and_citations(blocks)

        self.assertEqual(
            text[citations[0]["content_start"]:citations[0]["content_end"]],
            "Afirmación A.",
        )
        self.assertEqual(
            text[citations[1]["content_start"]:citations[1]["content_end"]],
            "Afirmación B.",
        )

    def test_several_citations_on_one_block_share_its_span(self):
        blocks = [
            FakeBlock(
                "text",
                "Dos fuentes lo sostienen.",
                [FakeCitation("uno", document_index=0), FakeCitation("dos", document_index=1)],
            ),
        ]
        text, citations = _text_and_citations(blocks)

        self.assertEqual(len(citations), 2)
        self.assertEqual(
            {(c["content_start"], c["content_end"]) for c in citations},
            {(0, len(text))},
        )

    def test_non_text_blocks_do_not_move_the_offsets(self):
        blocks = [
            FakeBlock("tool_use", ""),
            FakeBlock("text", "La meta es 2030.", [FakeCitation("meta")]),
        ]
        text, citations = _text_and_citations(blocks)

        self.assertEqual(citations[0]["content_start"], 0)
        self.assertEqual(text[citations[0]["content_start"]:], "La meta es 2030.")

    def test_a_response_without_citations_still_returns_its_text(self):
        text, citations = _text_and_citations([FakeBlock("text", "Sin citar nada.")])

        self.assertEqual(text, "Sin citar nada.")
        self.assertEqual(citations, [])


class ResolvedCitationCarriesTheAnchorTests(SimpleTestCase):
    """El ancla tiene que sobrevivir la resolución: es lo que se persiste."""

    def test_content_offsets_reach_the_resolved_citation(self):
        fuente = payload("ndc", ["La meta al 2030 es ambiciosa."])
        blocks = [
            FakeBlock("text", "Preámbulo. "),
            FakeBlock(
                "text",
                "El país fija una meta al 2030.",
                [
                    FakeCitation(
                        "La meta al 2030 es ambiciosa.",
                        start_char_index=fuente.text.index("La meta"),
                        end_char_index=fuente.text.index("La meta") + 29,
                    )
                ],
            ),
        ]
        text, raw = _text_and_citations(blocks)
        resolved = resolve_citations(raw, [fuente])

        self.assertEqual(len(resolved), 1)
        cita = resolved[0]
        self.assertEqual(
            text[cita["content_start"]:cita["content_end"]],
            "El país fija una meta al 2030.",
        )
        # La otra punta sigue apuntando al documento fuente.
        self.assertEqual(cita["document_slug"], "ndc")
        self.assertTrue(cita["verified"])

    def test_retry_replaces_the_citations_of_the_discarded_response(self):
        """Un paso tabular que reintenta descarta su primera respuesta.

        Las citas se recolectaban sólo del primer intento, así que el paso
        quedaba con las filas del reintento y las fuentes de la respuesta que se
        tiró — sin nada que lo delatara, porque las dos son citas válidas.
        """
        import json
        from types import SimpleNamespace
        from unittest.mock import patch

        from apps.skill.services import _coerce_with_retry

        schema = {
            "name": "Matriz",
            "columns": [
                {
                    "key": "resultado",
                    "label": "Resultado",
                    "type": "enum",
                    "required": True,
                    "allowed_values": ["Alineado"],
                }
            ],
        }
        citas = [{"cited_text": "de la primera respuesta", "document_index": 0}]

        def segundo_intento(messages, **kwargs):
            salida = kwargs.get("citations_out")
            if salida is not None:
                salida.append({"cited_text": "del reintento", "document_index": 0})
            return json.dumps({"rows": [{"resultado": "Alineado"}]}), {}, "claude-opus-5"

        with patch("apps.skill.services._call_model", side_effect=segundo_intento):
            table, content, _ = _coerce_with_retry(
                content='{"rows": [{"resultado": "Inventado"}]}',
                table_schema=schema,
                strict=True,
                messages=[{"role": "user", "content": "x"}],
                skill=SimpleNamespace(id=1),
                tier="deep",
                tool_ctx=None,
                step=SimpleNamespace(id=7),
                execution=SimpleNamespace(id=42),
                citations_out=citas,
            )

        self.assertEqual(table["rows"], [{"resultado": "Alineado"}])
        self.assertEqual(
            [c["cited_text"] for c in citas],
            ["del reintento"],
            "las citas del primer intento describen un texto que ya no existe",
        )

    def test_a_successful_first_attempt_keeps_its_citations(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch

        from apps.skill.services import _coerce_with_retry

        schema = {
            "name": "Matriz",
            "columns": [
                {
                    "key": "resultado",
                    "label": "Resultado",
                    "type": "enum",
                    "required": True,
                    "allowed_values": ["Alineado"],
                }
            ],
        }
        citas = [{"cited_text": "del primer intento", "document_index": 0}]

        with patch("apps.skill.services._call_model") as sin_uso:
            _coerce_with_retry(
                content=json.dumps({"rows": [{"resultado": "Alineado"}]}),
                table_schema=schema,
                strict=True,
                messages=[],
                skill=SimpleNamespace(id=1),
                tier="deep",
                tool_ctx=None,
                step=SimpleNamespace(id=7),
                execution=SimpleNamespace(id=42),
                citations_out=citas,
            )

        sin_uso.assert_not_called()
        self.assertEqual([c["cited_text"] for c in citas], ["del primer intento"])

    def test_a_citation_without_anchor_resolves_to_none_not_zero(self):
        """Una cita que llega sin offsets —de un camino que no los produce— no
        debe aterrizar en el carácter 0, que sería anclarla a la primera frase
        del informe."""
        fuente = payload("nap", ["Texto del plan."])
        raw = [{
            "cited_text": "Texto del plan.",
            "document_index": 0,
            "start_char_index": 0,
            "end_char_index": 15,
            "start_page_number": None,
            "end_page_number": None,
        }]
        resolved = resolve_citations(raw, [fuente])

        self.assertIsNone(resolved[0]["content_start"])
        self.assertIsNone(resolved[0]["content_end"])
