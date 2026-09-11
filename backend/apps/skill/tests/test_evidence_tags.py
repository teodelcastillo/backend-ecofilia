"""
Cómo elige un paso los documentos que lee.

Lo que se prueba acá es sobre todo lo que **no** tiene que pasar: que un paso
acotado a la NDC no termine leyendo el expediente entero cuando la operación no
tiene NDC, y que ningún modo pierda el documento principal. Las dos cosas eran
así antes de las etiquetas y ninguna se veía desde afuera.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument
from apps.project.services.evidence_tags import apply_default_tags
from apps.skill.models import (
    Skill,
    SkillStep,
    SkillType,
    StepEvidenceSelection,
)
from apps.skill.services import _resolve_step_documents, resolve_documents
from apps.skill.models import SkillExecution

User = get_user_model()


class StepEvidenceScopeTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="exec@example.com", password="secret123", username="exec"
        )
        self.project = Project.objects.create(owner=self.user, name="Operación X")

        self.ido = Document.objects.create(
            owner=self.user, name="IDO", slug="ido", extracted_text="texto ido"
        )
        self.ndc = Document.objects.create(
            owner=self.user,
            name="NDC Colombia",
            slug="ndc-colombia",
            topics=["ndcs: contribuciones determinadas a nivel nacional"],
            extracted_text="texto ndc",
        )
        self.anexo = Document.objects.create(
            owner=self.user, name="Anexo", slug="anexo", extracted_text="texto anexo"
        )

        # Las etiquetas de semilla ya existen (migración 0016): se toman las que
        # hay en vez de crear duplicados, que es también lo que pasa en
        # producción.
        self.tag_ndc, _ = EvidenceTag.objects.update_or_create(
            slug="ndc", defaults={"name": "NDC", "source_topics": ["ndcs"]}
        )
        self.tag_nap, _ = EvidenceTag.objects.update_or_create(
            slug="nap", defaults={"name": "NAP", "source_topics": ["naps"]}
        )

        for doc in (self.ido, self.ndc, self.anexo):
            ProjectDocument.objects.create(project=self.project, document=doc)
        self.project.blueprint_document = self.ido
        self.project.save(update_fields=["blueprint_document"])

        link = ProjectDocument.objects.get(project=self.project, document=self.ndc)
        link.tags.set([self.tag_ndc])

        self.skill = Skill.objects.create(
            name="Workflow", skill_type=SkillType.COPILOT, owner=self.user
        )
        self.execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        self.documents = resolve_documents(self.execution)

    def _scope(self, step, runtime=None):
        return _resolve_step_documents(
            step,
            self.documents,
            runtime,
            blueprint_id=self.project.blueprint_document_id,
            project_id=self.project.id,
        )

    def _slugs(self, scope):
        return sorted(scope.documents.values_list("slug", flat=True))

    def test_all_reads_the_whole_file(self):
        step = SkillStep.objects.create(
            skill=self.skill, title="Paso", instructions="x", position=1
        )
        self.assertEqual(self._slugs(self._scope(step)), ["anexo", "ido", "ndc-colombia"])

    def test_tagged_reads_only_its_tag_plus_the_blueprint(self):
        step = SkillStep.objects.create(
            skill=self.skill,
            title="CT M3",
            instructions="x",
            position=2,
            evidence_selection=StepEvidenceSelection.TAGGED,
            evidence_tags=["ndc"],
        )
        # El anexo queda afuera; el IDO entra aunque no esté etiquetado.
        self.assertEqual(self._slugs(self._scope(step)), ["ido", "ndc-colombia"])

    def test_tagged_adds_named_documents_on_top(self):
        step = SkillStep.objects.create(
            skill=self.skill,
            title="CT M3",
            instructions="x",
            position=3,
            evidence_selection=StepEvidenceSelection.TAGGED,
            evidence_tags=["ndc"],
            document_slugs=["anexo"],
        )
        self.assertEqual(
            self._slugs(self._scope(step)), ["anexo", "ido", "ndc-colombia"]
        )

    def test_missing_tag_does_not_fall_back_to_everything(self):
        """El caso que motivó el rediseño.

        Un paso acotado a una etiqueta que la operación no tiene se queda con
        el documento principal y **declara** la ausencia. Caer al expediente
        completo —lo que hacía el fallback de los slugs— invierte el pedido del
        autor sin que nadie se entere.
        """
        step = SkillStep.objects.create(
            skill=self.skill,
            title="Paso sin evidencia",
            instructions="x",
            position=4,
            evidence_selection=StepEvidenceSelection.TAGGED,
            evidence_tags=["nap"],
        )
        scope = self._scope(step)
        self.assertEqual(self._slugs(scope), ["ido"])
        self.assertEqual(
            scope.diagnostics["empty_reason"], "sin_documentos_etiquetados"
        )
        self.assertEqual(scope.diagnostics["tags_requested"], ["nap"])

    def test_blueprint_only(self):
        step = SkillStep.objects.create(
            skill=self.skill,
            title="Determinación",
            instructions="x",
            position=5,
            evidence_selection=StepEvidenceSelection.BLUEPRINT_ONLY,
        )
        self.assertEqual(self._slugs(self._scope(step)), ["ido"])

    def test_manual_keeps_the_blueprint(self):
        step = SkillStep.objects.create(
            skill=self.skill,
            title="Manual",
            instructions="x",
            position=6,
            evidence_selection=StepEvidenceSelection.MANUAL,
            document_slugs=["anexo"],
        )
        self.assertEqual(self._slugs(self._scope(step)), ["anexo", "ido"])

    def test_runtime_override_wins_over_the_definition(self):
        step = SkillStep.objects.create(
            skill=self.skill,
            title="CT M3",
            instructions="x",
            position=7,
            evidence_selection=StepEvidenceSelection.TAGGED,
            evidence_tags=["ndc"],
        )
        scope = self._scope(step, runtime=["anexo"])
        self.assertEqual(self._slugs(scope), ["anexo", "ido"])
        self.assertEqual(scope.diagnostics["selection"], "runtime_override")

    def test_tags_of_another_operation_do_not_leak(self):
        """La etiqueta vive en el vínculo, no en el documento.

        Si se resolviera por documento, un anexo etiquetado NDC en otra
        operación entraría acá — que es exactamente la confusión que la
        etiqueta-en-la-operación viene a evitar.
        """
        otra = Project.objects.create(owner=self.user, name="Operación Y")
        link = ProjectDocument.objects.create(project=otra, document=self.anexo)
        link.tags.set([self.tag_ndc])

        step = SkillStep.objects.create(
            skill=self.skill,
            title="CT M3",
            instructions="x",
            position=8,
            evidence_selection=StepEvidenceSelection.TAGGED,
            evidence_tags=["ndc"],
        )
        self.assertEqual(self._slugs(self._scope(step)), ["ido", "ndc-colombia"])

    def test_tagged_without_operation_reads_everything(self):
        """Un workflow etiquetado corriendo sobre un repositorio.

        No hay operación donde resolver las etiquetas. Leer todo es lo único
        que puede hacer, pero tiene que quedar dicho que el acotamiento no se
        aplicó.
        """
        step = SkillStep.objects.create(
            skill=self.skill,
            title="CT M3",
            instructions="x",
            position=9,
            evidence_selection=StepEvidenceSelection.TAGGED,
            evidence_tags=["ndc"],
        )
        scope = _resolve_step_documents(
            step, self.documents, [], blueprint_id=None, project_id=None
        )
        self.assertEqual(self._slugs(scope), ["anexo", "ido", "ndc-colombia"])
        self.assertEqual(scope.diagnostics["fallback"], "sin_operacion")


class DefaultTagsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="lib@example.com", password="secret123", username="lib"
        )
        self.project = Project.objects.create(owner=self.user, name="Operación")
        # Las dos formas, como en la semilla real (migración 0016): "ndcs" es
        # la carpeta pineada de la biblioteca CAF, "ndc" el singular que
        # cualquiera puede tipear a mano.
        EvidenceTag.objects.update_or_create(
            slug="ndc", defaults={"name": "NDC", "source_topics": ["ndcs", "ndc"]}
        )

    def test_topic_with_long_label_matches_by_acronym(self):
        """La biblioteca guarda «NDCS: Contribuciones…»; la etiqueta, «ndcs»."""
        doc = Document.objects.create(
            owner=self.user,
            name="NDC Perú",
            slug="ndc-peru",
            topics=["NDCS: Contribuciones Determinadas a Nivel Nacional"],
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        apply_default_tags(link)
        self.assertEqual(
            list(link.tags.values_list("slug", flat=True)), ["ndc"]
        )

    def test_document_without_topics_gets_none(self):
        doc = Document.objects.create(
            owner=self.user, name="Anexo", slug="anexo-2", topics=[]
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        apply_default_tags(link)
        self.assertEqual(link.tags.count(), 0)

    def test_existing_tags_are_not_overwritten(self):
        """Una decisión humana no se pisa al re-vincular."""
        otra, _ = EvidenceTag.objects.update_or_create(
            slug="nap", defaults={"name": "NAP", "source_topics": ["naps"]}
        )
        doc = Document.objects.create(
            owner=self.user, name="NDC", slug="ndc-2", topics=["ndcs"]
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        link.tags.set([otra])
        apply_default_tags(link)
        self.assertEqual(list(link.tags.values_list("slug", flat=True)), ["nap"])

    def test_topic_without_colon_still_matches_by_word(self):
        """
        El formato "ndcs: descripción" es el de las carpetas pineadas de la
        biblioteca CAF; nada obliga a que un bibliotecario lo respete. Un topic
        como "ndc colombia 2023" tiene que matchear igual.
        """
        doc = Document.objects.create(
            owner=self.user, name="NDC suelto", slug="ndc-suelto",
            topics=["ndc colombia 2023"],
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        apply_default_tags(link)
        self.assertEqual(link.tags.first().slug, "ndc")

    def test_hyphenated_topic_matches(self):
        doc = Document.objects.create(
            owner=self.user, name="NDC con guion", slug="ndc-guion",
            topics=["ndc-actualizada-2023"],
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        apply_default_tags(link)
        self.assertEqual(link.tags.first().slug, "ndc")

    def test_short_fragment_does_not_false_positive(self):
        """
        "ac" es una etiqueta real (Comunicación de Adaptación) de sólo dos
        letras. Un topic donde "ac" aparece pegado a otra palabra —no suelto
        como fragmento propio— no tiene que dispararla.
        """
        EvidenceTag.objects.update_or_create(
            slug="comunicacion-adaptacion",
            defaults={"name": "AC", "source_topics": ["ac"]},
        )
        doc = Document.objects.create(
            owner=self.user, name="Impacto ambiental", slug="impacto-ambiental",
            topics=["impacto ambiental y social"],
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        apply_default_tags(link)
        self.assertEqual(link.tags.count(), 0)

    def test_multiword_tag_source_topic_is_not_fragmented(self):
        """
        El lado de la etiqueta no se tokeniza: si se fragmentara igual que el
        documento, una etiqueta con source_topics=["comunicacion nacional"]
        matchearía cualquier topic que mencione "nacional" suelto —exactamente
        el falso positivo que la asimetría evita.
        """
        EvidenceTag.objects.update_or_create(
            slug="inventario-gei",
            defaults={"name": "Inventario GEI", "source_topics": ["comunicacion nacional"]},
        )
        doc = Document.objects.create(
            owner=self.user, name="Plan nacional de riego", slug="plan-nacional-riego",
            topics=["plan nacional de riego"],
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc)
        apply_default_tags(link)
        self.assertEqual(link.tags.count(), 0)


class IetEvidenceMappingTestCase(TestCase):
    """
    El mapeo del IET, tal como lo dejó la migración de datos.

    Se prueba contra la base migrada y no contra la lista de la migración
    porque lo que interesa es el resultado: que los pasos que deciden una
    determinación de alineación lean el instrumento que corresponde, y que los
    que integran criterios previos no reciban el expediente entero.

    Si alguien renombra un paso del IET desde el admin, la migración lo saltea
    y este test lo va a decir — que es exactamente cuándo hay que volver a
    mirarlo.
    """

    IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"

    def setUp(self):
        from apps.skill.models import Skill

        self.skill = Skill.objects.filter(slug=self.IET_SLUG).first()
        if self.skill is None:
            self.skipTest("El workflow del IET no está en esta base.")

    def _step(self, prefix):
        matches = [
            s for s in self.skill.steps.all() if (s.title or "").startswith(prefix)
        ]
        self.assertEqual(len(matches), 1, f"Paso «{prefix}» no encontrado o repetido")
        return matches[0]

    def test_consistencia_con_la_ndc_lee_la_ndc(self):
        """El caso canónico: CT M3 pide la NDC y nada más."""
        step = self._step("D.3. CT M3")
        self.assertEqual(step.evidence_selection, "tagged")
        self.assertEqual(step.evidence_tags, ["ndc"])

    def test_contexto_de_pais_lee_los_instrumentos_nacionales(self):
        step = self._step("B.1.")
        self.assertEqual(step.evidence_selection, "tagged")
        self.assertIn("ndc", step.evidence_tags)
        self.assertIn("nap", step.evidence_tags)

    def test_pasos_que_integran_no_reciben_biblioteca(self):
        """Las determinaciones y el resumen trabajan sobre lo ya redactado."""
        for prefix in ("D.2. Determinación", "D.3. Determinación", "E.1.", "E.3."):
            with self.subTest(paso=prefix):
                step = self._step(prefix)
                self.assertEqual(step.evidence_selection, "blueprint_only")
                self.assertEqual(step.evidence_tags, [])

    def test_todos_los_pasos_quedaron_declarados(self):
        """Ningún paso quedó leyendo todo el expediente por omisión."""
        sin_declarar = [
            s.title for s in self.skill.steps.all() if s.evidence_selection == "all"
        ]
        self.assertEqual(sin_declarar, [])

    def test_las_etiquetas_pedidas_existen_en_el_catalogo(self):
        """Un slug que no existe deja al paso sin evidencia para siempre."""
        pedidas = {t for s in self.skill.steps.all() for t in (s.evidence_tags or [])}
        existentes = set(
            EvidenceTag.objects.filter(slug__in=pedidas).values_list("slug", flat=True)
        )
        self.assertEqual(pedidas - existentes, set())
