"""
La migración que lleva las etiquetas de los vínculos a los documentos.

Corre sobre los datos de producción una sola vez, así que lo que se prueba es
que no pierda ninguna decisión humana y que no deje dos NDC en una operación.
"""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("project", "0011_evidence_tags_on_document"), ("document", "0018_evidence_tags_on_document")]
AFTER = [("project", "0012_move_evidence_tags_to_documents")]


class MoveEvidenceTagsMigrationTestCase(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(BEFORE)
        apps = executor.loader.project_state(BEFORE).apps

        Document = apps.get_model("document", "Document")
        EvidenceTag = apps.get_model("document", "EvidenceTag")
        Project = apps.get_model("project", "Project")
        ProjectDocument = apps.get_model("project", "ProjectDocument")

        # El usuario sale del modelo actual: el estado histórico de `user` en
        # este punto no conoce columnas que la tabla ya tiene.
        from django.contrib.auth import get_user_model

        owner_id = get_user_model().objects.create_user(
            email="m@example.com", password="x", username="m"
        ).id
        tag = lambda slug, topics: EvidenceTag.objects.update_or_create(  # noqa: E731
            slug=slug, defaults={"name": slug, "source_topics": topics}
        )[0]
        ndc = tag("ndc", ["ndcs", "ndc"])
        metodologia = tag("metodologia-caf", ["metodologia caf"])
        doc_op = tag("documento-operacion", [])
        salvaguardas = tag("salvaguardas-cesas", ["salvaguardas"])

        def doc(slug, topics=()):
            return Document.objects.create(owner_id=owner_id, name=slug, slug=slug, topics=list(topics))

        self.p1 = Project.objects.create(owner_id=owner_id, name="Op 1", slug="op-1")
        self.p2 = Project.objects.create(owner_id=owner_id, name="Op 2", slug="op-2")

        def link(project, document, tags=()):
            pd = ProjectDocument.objects.create(project=project, document=document)
            pd.tags.set(list(tags))
            return pd

        # Tema con acento: antes no matcheaba, ahora sí.
        self.metod = doc("guia", ["Metodología CAF"])
        link(self.p1, self.metod)
        # Etiquetado a mano igual en las dos operaciones → pasa al documento.
        self.perfil = doc("perfil")
        link(self.p1, self.perfil, [doc_op])
        link(self.p2, self.perfil, [doc_op])
        # Etiquetado distinto en cada operación → cada una conserva lo suyo.
        self.ias = doc("ias")
        link(self.p1, self.ias, [salvaguardas])
        link(self.p2, self.ias, [doc_op])
        # Dos versiones de la NDC: la vigente etiquetada, la vieja desetiquetada
        # por la sincronización de país.
        self.ndc_new = doc("ndc-2021", ["ndcs"])
        self.ndc_old = doc("ndc-2016", ["ndcs"])
        link(self.p1, self.ndc_new, [ndc])
        link(self.p1, self.ndc_old)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(AFTER)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _effective(self, project_slug, doc_slug):
        from apps.project.models import ProjectDocument
        from apps.project.services.evidence_tags import effective_tag_slugs

        link = ProjectDocument.objects.get(project__slug=project_slug, document__slug=doc_slug)
        return effective_tag_slugs(link)

    def _doc_tags(self, slug):
        from apps.document.models import Document

        return sorted(Document.objects.get(slug=slug).evidence_tags.values_list("slug", flat=True))

    def test_accented_topic_now_tags_the_document(self):
        self.assertEqual(self._doc_tags("guia"), ["metodologia-caf"])
        self.assertEqual(self._effective("op-1", "guia"), ["metodologia-caf"])

    def test_consistent_manual_tags_move_to_the_document(self):
        self.assertEqual(self._doc_tags("perfil"), ["documento-operacion"])
        self.assertEqual(self._effective("op-2", "perfil"), ["documento-operacion"])

    def test_divergent_manual_tags_stay_in_each_operation(self):
        self.assertEqual(self._doc_tags("ias"), [])
        self.assertEqual(self._effective("op-1", "ias"), ["salvaguardas-cesas"])
        self.assertEqual(self._effective("op-2", "ias"), ["documento-operacion"])

    def test_superseded_country_instrument_does_not_come_back(self):
        self.assertEqual(self._doc_tags("ndc-2016"), ["ndc"])
        self.assertEqual(self._effective("op-1", "ndc-2021"), ["ndc"])
        self.assertEqual(self._effective("op-1", "ndc-2016"), [])
