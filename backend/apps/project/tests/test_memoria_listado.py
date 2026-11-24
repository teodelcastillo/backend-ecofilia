"""
El listado y el detalle de operaciones no cargan el texto de los documentos.

El 20-nov-2026 murieron 19 workers de la API por memoria: ``ProjectViewSet``
traía, por cada documento vinculado a cada operación, el texto completo del PDF
(``extracted_text``) para descartarlo al serializar. Ninguna respuesta lo
muestra, así que ninguna consulta debe pedirlo.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.document.models import LIST_DEFERRED_COLUMNS, Document
from apps.project.models import Project, ProjectDocument, ProjectStageReport
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType

User = get_user_model()

TEXTO_LARGO = "texto del PDF " * 1000


class ProjectNoCargaTextoDeDocumentosTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="secret123", username="owner"
        )
        self.client.force_authenticate(self.owner)

    def _operacion(self, nombre: str, documentos: int) -> Project:
        docs = [
            Document.objects.create(
                owner=self.owner,
                name=f"{nombre} doc {i}",
                slug=f"{nombre}-doc-{i}",
                extracted_text=TEXTO_LARGO,
                content_summary=TEXTO_LARGO,
                last_error=TEXTO_LARGO,
            )
            for i in range(documentos)
        ]
        project = Project.objects.create(
            owner=self.owner, name=nombre, blueprint_document=docs[0]
        )
        for doc in docs:
            ProjectDocument.objects.create(project=project, document=doc)
        return project

    def _consultas(self, url: str):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response, [q["sql"] for q in ctx.captured_queries]

    def _sin_columnas_pesadas(self, sqls: list[str]):
        for column in LIST_DEFERRED_COLUMNS:
            culpables = [sql for sql in sqls if f'"document_document"."{column}"' in sql]
            self.assertEqual(culpables, [], f"una consulta trae {column}")

    def test_listado_no_trae_el_texto_y_sigue_mostrando_los_documentos(self):
        project = self._operacion("listado", 3)
        response, sqls = self._consultas(reverse("project-list"))

        self._sin_columnas_pesadas(sqls)
        fila = next(p for p in response.data if p["slug"] == project.slug)
        self.assertEqual(fila["blueprint_document_slug"], "listado-doc-0")
        self.assertEqual(
            sorted(d["slug"] for d in fila["documents"]),
            ["listado-doc-0", "listado-doc-1", "listado-doc-2"],
        )
        self.assertNotIn(b"texto del PDF", response.content)

    def test_detalle_no_trae_el_texto(self):
        project = self._operacion("detalle", 3)
        response, sqls = self._consultas(reverse("project-detail", args=[project.slug]))

        self._sin_columnas_pesadas(sqls)
        self.assertEqual(len(response.data["documents"]), 3)
        self.assertEqual(response.data["blueprint_document_slug"], "detalle-doc-0")

    def _informe(self, project: Project, n: int) -> ProjectStageReport:
        skill = Skill.objects.get_or_create(
            slug="informe-memoria",
            defaults={"name": "Informe", "skill_type": SkillType.QUICK, "owner": self.owner},
        )[0]
        pesada = {"output": TEXTO_LARGO, "document_snapshot": [{"texto": TEXTO_LARGO}],
                  "metadata": {"x": TEXTO_LARGO}}
        run = SkillExecution.objects.create(
            skill=skill, owner=self.owner, project=project,
            status=ExecutionStatus.COMPLETED, **pesada,
        )
        lectura = SkillExecution.objects.create(
            skill=skill, owner=self.owner, project=project,
            status=ExecutionStatus.COMPLETED, **pesada,
            output_structured={"steps": [
                {"title": "3. Consideraciones", "content": f"lectura {n}"},
            ]},
        )
        doc = Document.objects.create(
            owner=self.owner, name=f"informe {n}", slug=f"informe-{project.slug}-{n}",
            extracted_text=TEXTO_LARGO,
        )
        return ProjectStageReport.objects.create(
            project=project, stage=f"etapa_{n}", origin="guiado", execution=run,
            guided_execution=run, reader_execution=lectura, document=doc,
            created_by=self.owner,
        )

    def test_detalle_con_informes_no_trae_lo_pesado_de_las_corridas(self):
        project = self._operacion("informes", 2)
        self._informe(project, 1)
        url = reverse("project-detail", args=[project.slug])
        response, pocas = self._consultas(url)

        self._sin_columnas_pesadas(pocas)
        for sql in pocas:
            if "project_projectstagereport" in sql:
                for column in ("output", "document_snapshot", "metadata", "edited_output"):
                    self.assertNotIn(f'"skill_skillexecution"."{column}"', sql)
        informe = response.data["stage_reports"][0]
        self.assertEqual(informe["execution_status"], ExecutionStatus.COMPLETED)
        self.assertEqual(informe["execution_skill_slug"], "informe-memoria")
        self.assertEqual(informe["document"]["slug"], f"informe-{project.slug}-1")
        self.assertEqual(informe["reading"], {"consideraciones_adicionales": "lectura 1"})
        self.assertNotIn(b"texto del PDF", response.content)

        # Las columnas diferidas no se buscan después, informe por informe.
        self._informe(project, 2)
        self._informe(project, 3)
        _, muchas = self._consultas(url)
        self.assertEqual(len(muchas), len(pocas))

    def test_diferir_no_agrega_una_consulta_por_documento(self):
        # Si algún serializer leyera una columna diferida, Django la buscaría
        # fila por fila: el listado crecería con cada documento vinculado.
        self._operacion("chica", 1)
        _, pocas = self._consultas(reverse("project-list"))
        self._operacion("grande", 6)
        _, muchas = self._consultas(reverse("project-list"))

        self.assertEqual(len(muchas), len(pocas))
