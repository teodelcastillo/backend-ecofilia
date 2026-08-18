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
        # Un documento sin texto no genera bloque citable: no hay nada que citar.
        payloads = cb.build_document_payloads(
            plan, texts={1: documents[0].extracted_text}
        )
        self.assertEqual([p.slug for p in payloads], ["ndc"])


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


def paged(pages: list[str]) -> str:
    """Texto extraído tal como lo deja el parser: con marcadores de página."""
    return "\n\n".join(f"<<<PAGE:{i}>>>\n\n{t}" for i, t in enumerate(pages, start=1))


class DocumentBlockTests(SimpleTestCase):
    """Los documentos viajan como bloques citables, no como texto pegado.

    Es lo que habilita las citas nativas: la API sólo devuelve ubicación para
    lo que llegó dentro de un bloque ``document``.
    """

    def setUp(self):
        self.documents = [
            FakeDocument(1, "nap", "NAP", 3, paged(["Uno.", "Metas al 2030.", "Tres."])),
            FakeDocument(2, "ido", "IDO", 2, paged(["Alfa.", "Beta."])),
        ]
        self.texts = {d.id: d.extracted_text for d in self.documents}
        self.plan = cb.plan_context(self.documents, reserved_tokens=10_000, texts=self.texts)
        self.payloads = cb.build_document_payloads(self.plan, texts=self.texts)

    def test_page_markers_do_not_travel(self):
        """Si viajaran, aparecerían dentro del texto literal que devuelve la
        API y además correrían los offsets respecto del documento real."""
        self.assertTrue(all("<<<PAGE" not in p.text for p in self.payloads))
        self.assertTrue(all(p.has_pages for p in self.payloads))

    def test_each_full_document_becomes_a_citable_block(self):
        content = self._content()
        blocks = [b for b in content if b["type"] == "document"]

        self.assertEqual(len(blocks), 2)
        self.assertTrue(all(b["citations"] == {"enabled": True} for b in blocks))
        self.assertEqual(blocks[0]["source"]["type"], "text")
        self.assertTrue(blocks[0]["title"].startswith("[nap]"))

    def test_cache_breakpoint_sits_on_the_last_document(self):
        content = self._content()

        self.assertIn("cache_control", content[-2] if len(content) > 2 else content[1])
        self.assertNotIn("cache_control", content[-1])  # el prompt del paso
        self.assertNotIn("cache_control", content[0])   # el inventario

    def test_degraded_document_gets_no_citable_block(self):
        """De un puñado de fragmentos no salen offsets del documento, así que
        una cita sobre ellos apuntaría a la página equivocada."""
        documents = [
            FakeDocument(1, "nc4", "NC4", 530, paged(["x" * 2_500_000])),
            FakeDocument(2, "nap", "NAP", 2, paged(["Metas.", "Fin."])),
        ]
        texts = {d.id: d.extracted_text for d in documents}
        plan = cb.plan_context(documents, reserved_tokens=50_000, texts=texts)

        self.assertEqual([d.slug for d in plan.degraded], ["nc4"])
        self.assertEqual(
            [p.slug for p in cb.build_document_payloads(plan, texts=texts)], ["nap"]
        )
        self.assertIn("no** son citables", cb.render_inventory(plan))

    def test_stable_prefix_is_identical_across_steps(self):
        """La invariante que hace pagable el esquema, ahora sobre bloques.

        Si el prefijo difiere en un byte entre pasos, la caché no aplica y el
        corpus se cobra entero una vez por paso.
        """
        prefixes = []
        for step in range(17):
            content = self._content(
                volatile=f"fragmentos del paso {step}",
                step_prompt=f"instrucción {step}",
            )
            cut = max(i for i, b in enumerate(content) if "cache_control" in b)
            prefixes.append(repr(content[: cut + 1]))

        self.assertEqual(len(set(prefixes)), 1)

    def test_non_anthropic_provider_falls_back_to_inline_text(self):
        messages = cb.build_messages(
            system_prompt="SYS",
            inventory="INV",
            documents=self.payloads,
            corpus_volatile="FRAG",
            step_prompt="INSTR",
            model="gpt-4o-mini",
        )

        self.assertIsInstance(messages[1]["content"], str)
        self.assertIn("Metas al 2030.", messages[1]["content"])

    def _content(self, *, volatile: str = "", step_prompt: str = "INSTRUCCIÓN"):
        return cb.build_messages(
            system_prompt="SYS",
            inventory=cb.render_inventory(self.plan),
            documents=self.payloads,
            corpus_volatile=volatile,
            step_prompt=step_prompt,
            model="claude-sonnet-5",
        )[1]["content"]


class BlueprintRoleTests(SimpleTestCase):
    """El documento principal no es uno más con prioridad de presupuesto.

    Describe la operación que se evalúa; los demás son el marco contra el que
    se la evalúa. Aplanarlos invita a confundir "la operación no hace X" con
    "el marco no exige X" — hallazgos distintos, presentados con la misma
    seguridad.
    """

    def setUp(self):
        self.documents = [
            make_document(1, "nap", 150),
            make_document(2, "ido", 40),     # el documento de la operación
            make_document(3, "nbsap", 120),
        ]
        self.plan = cb.plan_context(
            self.documents, reserved_tokens=10_000, blueprint_id=2
        )

    def test_plan_carries_the_role(self):
        self.assertEqual(self.plan.blueprint.slug, "ido")
        self.assertEqual(self.plan.diagnostics()["blueprint"], "ido")

    def test_inventory_separates_subject_from_yardstick(self):
        inventory = cb.render_inventory(self.plan)

        self.assertIn("Documento de la operación", inventory)
        self.assertIn("Marco de referencia", inventory)
        # El principal encabeza, aunque en la lista venga segundo.
        self.assertLess(inventory.index("[ido]"), inventory.index("[nap]"))

    def test_inventory_explains_what_an_absence_means_in_each_group(self):
        inventory = cb.render_inventory(self.plan)

        self.assertIn("sobre la operación", inventory)
        self.assertIn("sobre el marco", inventory)

    def test_single_document_is_not_grouped(self):
        """Con un solo documento la separación no informa nada y sólo agrega
        ruido al prefijo cacheado."""
        plan = cb.plan_context([make_document(1, "ido", 40)],
                               reserved_tokens=10_000, blueprint_id=1)

        self.assertNotIn("Marco de referencia", cb.render_inventory(plan))

    def test_without_a_blueprint_the_list_stays_flat(self):
        plan = cb.plan_context(self.documents, reserved_tokens=10_000)

        self.assertIsNone(plan.blueprint)
        self.assertNotIn("Documento de la operación", cb.render_inventory(plan))

    def test_blueprint_is_never_the_one_degraded(self):
        """No es "se degrada último": no es candidato. Un informe sobre
        fragmentos del resto es un informe incompleto; uno sobre fragmentos del
        principal es un informe sobre otra cosa."""
        big = [
            make_document(1, "ido", 1_400),   # principal y además el más grande
            make_document(2, "nap", 1_300),
        ]
        plan = cb.plan_context(big, reserved_tokens=50_000, blueprint_id=1)
        by_slug = {d.slug: d.mode for d in plan.deliveries}

        self.assertEqual(by_slug["nap"], cb.PARTIAL)
        self.assertEqual(by_slug["ido"], cb.FULL)
        self.assertFalse(plan.diagnostics()["blueprint_degraded"])

    def test_blueprint_survives_while_others_absorb_the_pressure(self):
        """Con cuatro documentos que no entran juntos, la presión la absorben
        los del marco. Cuántos caigan depende del tamaño —el planificador para
        apenas entra— pero el principal nunca está entre ellos."""
        big = [make_document(i, f"doc{i}", 900) for i in range(1, 5)]
        plan = cb.plan_context(big, reserved_tokens=50_000, blueprint_id=2)

        self.assertEqual(plan.blueprint.mode, cb.FULL)
        self.assertNotIn("doc2", [d.slug for d in plan.degraded])
        self.assertGreater(len(plan.degraded), 0, "el fixture tiene que forzar degradación")
        self.assertLessEqual(plan.corpus_tokens, plan.budget_tokens)

    def test_blueprint_degrades_only_when_physics_forces_it(self):
        """Si ni él solo entra, no hay regla que lo salve — pero queda
        registrado aparte, porque no es un degradado más."""
        huge = [make_document(1, "ido", 3_000)]  # ~1,3M tokens, sobre la ventana
        plan = cb.plan_context(huge, reserved_tokens=50_000, blueprint_id=1)

        self.assertEqual(plan.blueprint.mode, cb.PARTIAL)
        self.assertTrue(plan.diagnostics()["blueprint_degraded"])
        self.assertIn("ni siendo el documento principal", plan.blueprint.reason)

    def test_inventory_shouts_when_the_subject_is_incomplete(self):
        huge = [
            make_document(1, "ido", 3_000),
            make_document(2, "nap", 100),
        ]
        plan = cb.plan_context(huge, reserved_tokens=50_000, blueprint_id=1)

        self.assertIn("sólo fragmentos", cb.render_inventory(plan))
        self.assertIn("limitación del análisis", cb.render_inventory(plan))
