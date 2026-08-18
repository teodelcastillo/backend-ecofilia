"""
El presupuesto de contexto y la frontera de caché.

No tocan la base: ``context_budget`` trabaja sobre cualquier objeto que tenga
``id``/``slug``/``name``/``extracted_text``, así que los documentos son objetos
de prueba. Eso permite ejercitar corpus de un millón de tokens sin fixtures.

El test que importa es ``test_stable_prefix_is_identical_across_steps``. Todo
el resto describe el reparto; ése protege el mecanismo que lo hace pagable, y
existe porque ya se rompió una vez: los fragmentos del documento degradado
viajaban dentro del bloque cacheado, cambiaban en cada paso e invalidaban la
caché en los diecisiete.
"""
from dataclasses import dataclass

from django.test import SimpleTestCase

from apps.skill import context_budget as cb


@dataclass
class FakeDocument:
    id: int
    slug: str
    name: str
    page_count: int
    extracted_text: str


def make_document(doc_id: int, slug: str, kchars: int, pages: int = 100) -> FakeDocument:
    return FakeDocument(doc_id, slug, slug.upper(), pages, "x" * (kchars * 1000))


class PlanContextTests(SimpleTestCase):
    def test_small_corpus_travels_whole(self):
        documents = [
            make_document(1, "ndc", 200),
            make_document(2, "nap", 150),
            make_document(3, "ido", 50),
        ]
        plan = cb.plan_context(documents, reserved_tokens=50_000)

        self.assertTrue(all(d.mode == cb.FULL for d in plan.deliveries))
        self.assertTrue(plan.complete)
        self.assertEqual(cb.render_partials(plan, partial_blocks={}), "")

    def test_only_the_largest_document_is_degraded(self):
        """Degradar el expediente entero cuando sobra un documento es la
        respuesta vieja. Acá se degrada el que sobra y nadie más."""
        documents = [
            make_document(1, "btr", 1_400),
            make_document(2, "ndc", 550),
            make_document(3, "nap", 250),
            make_document(4, "ido", 80),
        ]
        plan = cb.plan_context(documents, reserved_tokens=50_000)

        self.assertEqual([d.slug for d in plan.degraded], ["btr"])
        self.assertEqual(sum(1 for d in plan.deliveries if d.mode == cb.FULL), 3)
        self.assertLessEqual(plan.corpus_tokens, plan.budget_tokens)

    def test_blueprint_is_degraded_last(self):
        documents = [
            make_document(1, "principal", 1_400),
            make_document(2, "ndc", 1_300),
            make_document(3, "nap", 250),
        ]
        plan = cb.plan_context(documents, reserved_tokens=50_000, blueprint_id=1)
        by_slug = {d.slug: d.mode for d in plan.deliveries}

        self.assertEqual(by_slug["ndc"], cb.PARTIAL)
        self.assertEqual(by_slug["principal"], cb.FULL)

    def test_step_reserve_shrinks_the_document_budget(self):
        documents = [make_document(1, "a", 900), make_document(2, "b", 900)]

        roomy = cb.plan_context(documents, reserved_tokens=20_000)
        tight = cb.plan_context(documents, reserved_tokens=500_000)

        self.assertLess(tight.budget_tokens, roomy.budget_tokens)
        self.assertGreater(len(tight.degraded), len(roomy.degraded))

    def test_document_without_text_is_declared_unusable(self):
        documents = [
            make_document(1, "ndc", 100),
            FakeDocument(2, "vacio", "VACIO", 10, ""),
        ]
        plan = cb.plan_context(documents, reserved_tokens=10_000)

        self.assertEqual([d.slug for d in plan.unavailable], ["vacio"])
        self.assertIn("NO DISPONIBLE", cb.render_inventory(plan))
        self.assertIn(
            "no puede usarse como fuente",
            cb.render_corpus(plan, texts={1: documents[0].extracted_text}),
        )


class InventoryTests(SimpleTestCase):
    """El inventario es lo que le permite al modelo distinguir "no está en el
    expediente" de "no me llegó". Es la mitad del producto, no un encabezado."""

    def test_complete_scope_allows_asserting_absence(self):
        plan = cb.plan_context([make_document(1, "ndc", 100)], reserved_tokens=10_000)
        self.assertIn("podés afirmarlo", cb.render_inventory(plan))

    def test_degraded_scope_forbids_asserting_absence(self):
        documents = [make_document(1, "btr", 1_400), make_document(2, "ndc", 300)]
        plan = cb.plan_context(documents, reserved_tokens=50_000)
        inventory = cb.render_inventory(plan)

        self.assertIn("[btr]", inventory)
        self.assertIn("nunca", inventory)

    def test_documents_outside_the_scope_may_not_be_cited(self):
        plan = cb.plan_context([make_document(1, "nap", 100)], reserved_tokens=10_000)
        self.assertIn("no lo tenés", cb.render_inventory(plan))


class CacheBoundaryTests(SimpleTestCase):
    """La frontera entre lo que se paga una vez y lo que se paga por paso."""

    def setUp(self):
        self.documents = [
            make_document(1, "btr", 1_400),
            make_document(2, "ndc", 550),
            make_document(3, "nap", 250),
        ]
        self.texts = {d.id: d.extracted_text for d in self.documents}
        self.plan = cb.plan_context(self.documents, reserved_tokens=50_000, texts=self.texts)

    def test_degraded_document_keeps_its_slot_without_its_text(self):
        rendered = cb.render_corpus(self.plan, texts=self.texts)
        block = rendered.split("===== DOCUMENTO 1/3")[1].split("===== FIN DOCUMENTO 1/3")[0]

        self.assertNotIn("x" * 1000, block)
        self.assertLess(len(block), 500)
        self.assertIn("DOCUMENTO 3/3", rendered)  # la numeración no se corre

    def test_whole_document_carries_its_text(self):
        rendered = cb.render_corpus(self.plan, texts=self.texts)
        block = rendered.split("===== DOCUMENTO 2/3")[1].split("===== FIN DOCUMENTO 2/3")[0]

        self.assertIn("x" * 100_000, block)

    def test_stable_part_does_not_depend_on_the_fragments(self):
        baseline = cb.render_corpus(self.plan, texts=self.texts)

        first = cb.render_partials(self.plan, partial_blocks={1: "fragmento del paso A"})
        second = cb.render_partials(self.plan, partial_blocks={1: "otro fragmento, paso B"})

        self.assertEqual(baseline, cb.render_corpus(self.plan, texts=self.texts))
        self.assertNotEqual(first, second)

    def test_stable_prefix_is_identical_across_steps(self):
        """La invariante que hace pagable el esquema.

        Si el prefijo difiere en un byte entre pasos, la caché no aplica y el
        corpus se cobra entero una vez por paso: sobre la operación 34 son
        ~19 USD por corrida en vez de ~4.
        """
        prefixes = [
            cb.build_messages(
                system_prompt="SYS",
                corpus_stable=cb.render_corpus(
                    cb.plan_context(self.documents, reserved_tokens=50_000, texts=self.texts),
                    texts=self.texts,
                ),
                corpus_volatile=f"fragmentos distintos del paso {step}",
                step_prompt=f"instrucción del paso {step}",
                model="claude-sonnet-5",
            )[1]["content"][0]
            for step in range(17)
        ]

        self.assertEqual(len({repr(p) for p in prefixes}), 1)


class BuildMessagesTests(SimpleTestCase):
    def _content(self, **kwargs):
        params = {
            "system_prompt": "SYS",
            "corpus_stable": "DOCUMENTOS COMPLETOS",
            "corpus_volatile": "FRAGMENTOS DEL PASO",
            "step_prompt": "INSTRUCCIÓN",
            "model": "claude-sonnet-5",
        }
        params.update(kwargs)
        return cb.build_messages(**params)

    def test_cache_breakpoint_sits_between_stable_and_volatile(self):
        content = self._content()[1]["content"]

        self.assertEqual(len(content), 3)
        self.assertEqual(content[0]["text"], "DOCUMENTOS COMPLETOS")
        self.assertIn("cache_control", content[0])
        self.assertEqual(content[1]["text"], "FRAGMENTOS DEL PASO")
        self.assertNotIn("cache_control", content[1])
        self.assertNotIn("cache_control", content[2])

    def test_no_empty_block_when_everything_fits(self):
        content = self._content(corpus_volatile="")[1]["content"]

        self.assertEqual(len(content), 2)
        self.assertTrue(all(block["text"] for block in content))

    def test_non_anthropic_provider_falls_back_to_inline_text(self):
        message = self._content(model="gpt-4o-mini")[1]

        self.assertIsInstance(message["content"], str)
        self.assertLess(
            message["content"].index("DOCUMENTOS COMPLETOS"),
            message["content"].index("INSTRUCCIÓN"),
        )

    def test_synthesis_step_without_corpus(self):
        message = self._content(corpus_stable="", corpus_volatile="")[1]
        self.assertEqual(message["content"], "INSTRUCCIÓN")
