"""
De IDO a DEC (decisiones del 2026-11-20).

- Si la operación hizo su IDO en Ecofilia, el informe de DEC no rehace el
  contexto de clima: arranca con los pasos «B.» copiados del IDO (con lo que
  publicó el ejecutivo). Sin IDO, corre completo.
- El DEC lee el IET IDO emitido: se vincula a la operación con su etiqueta.
- La mirada del ejecutivo de IDO se congela al pasar a DEC y nadie la pisa.
- La foto del cambio de etapa no cuenta como resultado de la etapa nueva; la
  emisión deja la foto oficial.
- La conclusión de Ecofilia por CT sale de la línea «**Conclusión:**» del
  informe visible, con S.1 de respaldo.
- El tablero lee un resumen por etapa con lo del ejecutivo.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import (
    Project,
    ProjectAlignmentSnapshot,
    ProjectDocument,
    ProjectShareRole,
    ProjectStageReport,
)
from apps.project.services import stage_inheritance
from apps.project.services.caf_criteria import cierre_del_criterio, ecofilia_determinations
from apps.project.services.stages import IET_SLUG, launch_stage_workflows
from apps.skill.models import ExecutionSectionEdit, ExecutionStatus, Skill, SkillExecution, SkillStep
from apps.user.institution_config import validate_config
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()
DELAY = "apps.skill.dispatch.run_skill_task.delay"
DOC_DISPATCH = "apps.document.signals.dispatch_processing"
DEC_SLUG = "dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion"
HEREDA = {"de": "ido_cci", "prefijo": "B.", "etiqueta": "iet-datbc-ido"}


class _Base(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        EvidenceTag.objects.get_or_create(slug="iet-datbc-ido", organization=self.caf, defaults={"name": "IET IDO"})
        self.owner = User.objects.create_user(email="owner@caf.com", password="x", organization=self.caf)
        self.editor = User.objects.create_user(email="editor@caf.com", password="x", organization=self.caf)
        self.otro = User.objects.create_user(email="m@otra.org", password="x")
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_staff=True, mfa_enabled=True
        )
        self.iet = Skill.objects.get(slug=IET_SLUG)
        self.dec = Skill.objects.create(
            name="IET DEC", slug=DEC_SLUG, skill_type=self.iet.skill_type,
            allowed_contexts=["project"], scope="organization", organization=self.caf,
        )
        for position, title in enumerate(
            ["B.1. Marco de políticas", "B.2. Perfil de riesgos", "A.3. Misión de Evaluación",
             "D.1.1. Criterio Técnico 1 (CT A1): Análisis de riesgos climáticos"],
            start=1,
        ):
            SkillStep.objects.create(skill=self.dec, title=title, position=position, instructions="x")
        config = caf_config()
        for stage in config["stages"]:
            if stage["slug"] == "dec":
                stage.update(report=DEC_SLUG, launches=[DEC_SLUG], hereda=dict(HEREDA))
        self.caf.config = config
        self.caf.save(update_fields=["config"])

        self.project = Project.objects.create(
            owner=self.owner, organization=self.caf, name="Operación",
            context_notes={"estado": "ido_cci", "pais": "Perú"},
        )
        self.project.shares.create(user=self.editor, role=ProjectShareRole.EDITOR)
        self.blueprint = Document.objects.create(
            owner=self.owner, name="Documento IDO", slug="doc-ido", chunking_status="done", extracted_text="x",
        )
        ProjectDocument.objects.create(project=self.project, document=self.blueprint, is_primary=True)
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        self.ido_run = SkillExecution.objects.create(
            skill=self.iet, owner=self.owner, project=self.project, status=ExecutionStatus.COMPLETED,
            stage="ido_cci", finished_at=timezone.now(),
            output_structured={"steps": [
                {"step_id": 901, "title": "B.1. Marco de políticas", "content": "Políticas del IDO.", "citations": []},
                {"step_id": 902, "title": "B.2. Perfil de riesgos", "content": "Riesgos del IDO.", "citations": []},
                {"step_id": 903, "title": "C.1. Co-beneficios", "content": "Actividades."},
            ]},
        )

    def _determinar(self):
        self.project.context_notes = {
            **self.project.context_notes,
            "alineacion_paris": {"ct_a1": "Riesgo medio", "objetivo_1": "Con brechas por CT A2"},
            "alineacion_fundamentos": {"ct_a1": "El APRC lo califica medio."},
            "registro_trazabilidad": {"participacion": ["solicitud"]},
        }
        self.project.save(update_fields=["context_notes"])

    def _emitir_ido(self, markdown="# IET IDO\n\nDeterminación del ejecutivo."):
        return ProjectStageReport.objects.create(
            project=self.project, stage="ido_cci", origin="editado", created_by=self.owner,
            execution=self.ido_run, emitted_at=timezone.now(), emitted_by=self.owner,
            emitted_markdown=markdown, emitted_assessment={"determinaciones": {"ct_a1": "Riesgo medio"}},
        )

    def _a_dec(self, generar=False):
        """Pasa a DEC; con ``generar``, también el «Generar» del alta.

        Pasar de IDO a DEC no lanza el análisis: la etapa se configura primero
        (documento DEC y complementarios) y el alta lo lanza al final.
        """
        self.client.force_authenticate(self.editor)
        with patch(DELAY), patch(DOC_DISPATCH), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/projects/{self.project.slug}/stage/", {"to": "dec"}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            if generar:
                response = self.client.post(f"/api/projects/{self.project.slug}/stage/launch/", {}, format="json")
                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        return response


class ConfiguracionTestCase(_Base):
    def test_hereda_is_validated(self):
        config = caf_config()
        for stage in config["stages"]:
            if stage["slug"] == "dec":
                stage["hereda"] = {"de": "aprobada", "prefijo": "", "otra": 1, "etiqueta": "no-existe"}
        _, errors = validate_config(config, organization=self.caf)
        texto = " ".join(errors)
        for esperado in ("hereda.de", "hereda.prefijo", "claves desconocidas", "hereda.etiqueta"):
            self.assertIn(esperado, texto)

    def test_a_valid_hereda_is_kept(self):
        from apps.user.institution_config import _stages

        errors: list[str] = []
        stages = _stages(self.caf.config["stages"], {"ido", "dec", "iet-datbc-ido"}, set(), errors)
        self.assertEqual([e for e in errors if "hereda" in e], [])
        self.assertEqual(next(s for s in stages if s["slug"] == "dec")["hereda"], HEREDA)


class HerenciaDelContextoTestCase(_Base):
    def test_dec_starts_with_the_context_of_ido_and_the_executive_edits(self):
        self._emitir_ido()
        ExecutionSectionEdit.objects.create(
            execution=self.ido_run, step_id=902, published="Riesgos corregidos por el ejecutivo."
        )
        self._a_dec()
        pasos = stage_inheritance.pasos_heredados(self.project, "dec", self.dec)
        self.assertEqual([p["title"] for p in pasos], ["B.1. Marco de políticas", "B.2. Perfil de riesgos"])
        self.assertEqual(pasos[0]["content"], "Políticas del IDO.")
        self.assertEqual(pasos[1]["content"], "Riesgos corregidos por el ejecutivo.")
        b1 = self.dec.steps.get(position=1)
        self.assertEqual(pasos[0]["step_id"], b1.id)
        self.assertEqual(pasos[0]["heredado_de"], {"execution_id": self.ido_run.id, "stage": "ido_cci"})

    def test_the_run_is_created_with_the_inherited_steps(self):
        from apps.skill.launch import launch_institutional

        self._emitir_ido()
        self._a_dec()
        pasos = stage_inheritance.pasos_heredados(self.project, "dec", self.dec)
        with patch(DELAY), patch("apps.skill.preflight.check_operation_run") as preflight, \
                self.captureOnCommitCallbacks(execute=True):
            preflight.return_value.blocking = []
            preflight.return_value.warnings = []
            outcome = launch_institutional(
                self.dec, owner=self.owner, project=self.project, stage="dec",
                launched_by="stage", prefilled_steps=pasos,
            )
        run = outcome.execution
        run.refresh_from_db()
        self.assertEqual(run.steps_completed, 2)
        self.assertEqual(len(run.output_structured["steps"]), 2)

    def test_without_ido_dec_runs_complete(self):
        self.project.context_notes = {"estado": "dec"}
        self.project.save(update_fields=["context_notes"])
        self.ido_run.delete()
        self.assertIsNone(stage_inheritance.pasos_heredados(self.project, "dec", self.dec))

    def test_context_steps_that_are_not_first_are_not_inherited(self):
        self._emitir_ido()
        self._a_dec()
        SkillStep.objects.create(skill=self.dec, title="B.5. Elementos clave", position=9, instructions="x")
        self.assertIsNone(stage_inheritance.pasos_heredados(self.project, "dec", self.dec))

    def test_if_ido_lacks_a_context_step_nothing_is_inherited(self):
        self._emitir_ido()
        self._a_dec()
        self.ido_run.output_structured = {"steps": self.ido_run.output_structured["steps"][:1]}
        self.ido_run.save(update_fields=["output_structured"])
        self.assertIsNone(stage_inheritance.pasos_heredados(self.project, "dec", self.dec))


class InformeIdoComoEvidenciaTestCase(_Base):
    def test_entering_dec_links_the_emitted_ido_report_with_its_tag(self):
        self._emitir_ido()
        response = self._a_dec(generar=True)
        link = ProjectDocument.objects.get(project=self.project, document__source__startswith="informe-etapa:")
        self.assertIn("iet-datbc-ido", list(link.document.evidence_tags.values_list("slug", flat=True)))
        self.assertTrue(link.document.file.name.endswith(".txt"))
        self.assertEqual(link.document.file.read().decode("utf-8"), "# IET IDO\n\nDeterminación del ejecutivo.")
        # Recién vinculado se procesa: el DEC espera (y se retoma al terminar).
        self.assertEqual(response.data["launches"][0]["status"], "blocked")

    def test_linking_is_idempotent_and_a_new_version_replaces_the_old(self):
        primero = self._emitir_ido()
        self._a_dec(generar=True)
        with patch(DOC_DISPATCH):
            stage_inheritance.vincular_informe_anterior(self.project, "dec", self.owner)
        self.assertEqual(
            ProjectDocument.objects.filter(project=self.project, document__source__startswith="informe-etapa:").count(), 1
        )
        primero.superseded_at = timezone.now()
        primero.save(update_fields=["superseded_at"])
        self._emitir_ido("# IET IDO v2")
        with patch(DOC_DISPATCH):
            documento = stage_inheritance.vincular_informe_anterior(self.project, "dec", self.owner)
        links = ProjectDocument.objects.filter(project=self.project, document__source__startswith="informe-etapa:")
        self.assertEqual([link.document_id for link in links], [documento.id])

    def test_an_uploaded_ido_report_links_its_own_document(self):
        subido = Document.objects.create(owner=self.owner, name="IET IDO firmado", slug="iet-ido", chunking_status="done")
        ProjectStageReport.objects.create(
            project=self.project, stage="ido_cci", origin="subido", document=subido, created_by=self.owner,
            emitted_at=timezone.now(), emitted_by=self.owner,
        )
        self._a_dec(generar=True)
        self.assertTrue(ProjectDocument.objects.filter(project=self.project, document=subido).exists())


class InformeIdoNoEsEvidenciaTestCase(_Base):
    """El IET IDO le llega al análisis DEC aparte, como referencia."""

    def test_the_linked_report_and_an_uploaded_one_are_previous_stage_reports(self):
        self._emitir_ido()
        self._a_dec(generar=True)
        vinculado = ProjectDocument.objects.get(project=self.project, document__source__startswith="informe-etapa:")
        tag = vinculado.document.evidence_tags.get(slug="iet-datbc-ido")
        externo = Document.objects.create(owner=self.owner, name="IET IDO de otra firma", slug="iet-externo")
        externo.evidence_tags.add(tag)
        ProjectDocument.objects.create(project=self.project, document=externo)
        otro = Document.objects.create(owner=self.owner, name="DEC", slug="dec-doc")
        ProjectDocument.objects.create(project=self.project, document=otro)

        self.assertEqual(
            stage_inheritance.informes_de_etapas_anteriores(self.project),
            {vinculado.document_id, externo.id},
        )


class MiradaPorEtapaTestCase(_Base):
    def test_the_ido_view_is_frozen_when_moving_to_dec(self):
        self._determinar()
        self._emitir_ido()
        self._a_dec()
        notes = self.project.context_notes
        congelada = notes["por_etapa"]["ido_cci"]
        self.assertEqual(congelada["assessment"]["determinaciones"]["ct_a1"], "Riesgo medio")
        self.assertEqual(congelada["notas"]["registro_trazabilidad"], {"participacion": ["solicitud"]})
        self.assertEqual(notes["precarga_de"], "ido_cci")
        # DEC arranca con una copia de la determinación, pero su registro es propio.
        self.assertEqual(notes["alineacion_paris"]["ct_a1"], "Riesgo medio")
        self.assertNotIn("registro_trazabilidad", notes)

    def test_editing_in_dec_does_not_touch_the_frozen_ido(self):
        self._determinar()
        self._emitir_ido()
        self._a_dec()
        notes = dict(self.project.context_notes)
        notes["alineacion_paris"] = {"ct_a1": "Riesgo alto"}
        notes["por_etapa"] = {"ido_cci": {"assessment": {"determinaciones": {"ct_a1": "trucho"}}}}
        self.client.force_authenticate(self.editor)
        response = self.client.patch(
            f"/api/projects/{self.project.slug}/", {"context_notes": notes}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["alineacion_paris"]["ct_a1"], "Riesgo alto")
        self.assertEqual(
            self.project.context_notes["por_etapa"]["ido_cci"]["assessment"]["determinaciones"]["ct_a1"],
            "Riesgo medio",
        )


class FotosTestCase(_Base):
    def test_the_transition_photo_is_marked(self):
        self._determinar()
        self._emitir_ido()
        self._a_dec()
        foto = ProjectAlignmentSnapshot.objects.filter(project=self.project, estado="dec").latest("id")
        self.assertEqual(foto.source, ProjectAlignmentSnapshot.Source.TRANSITION)

    def test_emitting_leaves_the_official_photo(self):
        from apps.project.services.stage_reports import emit

        self._determinar()
        report = emit(self.project, self.editor, "# IET")
        foto = ProjectAlignmentSnapshot.objects.get(project=self.project, report=report)
        self.assertEqual((foto.source, foto.estado), (ProjectAlignmentSnapshot.Source.EMISSION, "ido_cci"))
        self.assertEqual(foto.alineacion["ct_a1"], "Riesgo medio")


class ConclusionDelInformeTestCase(_Base):
    def test_the_closing_line_is_read(self):
        conclusion, faltante, cuerpo = cierre_del_criterio(
            "Análisis del APRC [[c:1]].\n\n**Conclusión:** «Riesgo medio».\n**Información faltante:** El APRC del sitio."
        )
        self.assertEqual((conclusion, faltante), ("Riesgo medio", "El APRC del sitio"))
        self.assertEqual(cuerpo, "Análisis del APRC [[c:1]].")

    def test_the_closing_line_wins_over_s1_and_dec_without_s1_has_conclusions(self):
        run = SkillExecution(output_structured={"steps": [
            {"title": "D.1.1. Criterio Técnico 1 (CT A1): Análisis de riesgos climáticos",
             "content": "Texto.\n\n**Conclusión:** riesgo bajo\n**Información faltante:** Ninguna"},
            {"title": "D.2.1. Criterio Técnico 1 (CT M1): Actividades no alineadas",
             "content": "Texto.\n\n**Conclusión:** Sin actividades no alineadas"},
            {"title": "D.2.2. Criterio Técnico 2 (CT M2): Actividades alineadas",
             "content": "Texto.\n\n**Conclusión:** Contiene actividades alineadas"},
            {"title": "D.1.2. Criterio Técnico 2 (CT A2): Análisis de medidas",
             "content": "Texto.\n\n**Conclusión:** Una opción que no existe"},
            {"title": "S.1. Síntesis por criterio", "table": {"rows": [
                {"criterio": "CT A1 — Análisis de riesgos climáticos", "determinacion": "Riesgo alto"},
                {"criterio": "CT A2 — Análisis de medidas", "determinacion": "Suficientes"},
            ]}},
        ]})
        out = ecofilia_determinations(run)
        self.assertEqual(out["ct_a1"]["determinacion"], "Riesgo bajo")
        self.assertEqual(out["ct_a1"]["informacion_faltante"], "")
        # Una conclusión que no es opción del CT no pisa S.1.
        self.assertEqual(out["ct_a2"]["determinacion"], "Suficientes")
        self.assertEqual(out["objetivo_2"]["determinacion"], "Alineada en el CT M2")


class StageSummaryTestCase(_Base):
    URL = "/api/projects/stage-summary/"

    def test_needs_the_institution_and_only_your_own(self):
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(self.URL).status_code, status.HTTP_400_BAD_REQUEST)
        self.client.force_authenticate(self.otro)
        self.assertEqual(self.client.get(self.URL, {"organization": "caf"}).status_code, status.HTTP_403_FORBIDDEN)

    def test_it_shows_the_executive_per_stage(self):
        self._determinar()
        self._emitir_ido()
        self._a_dec()
        self.client.force_authenticate(self.owner)
        response = self.client.get(self.URL, {"organization": "caf"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        fila = next(f for f in response.data if f["project_slug"] == self.project.slug)
        self.assertEqual(fila["estado"], "dec")
        ido = next(e for e in fila["etapas"] if e["stage"] == "ido_cci")
        dec = next(e for e in fila["etapas"] if e["stage"] == "dec")
        self.assertEqual(ido["informe"]["estado"], "emitido")
        self.assertEqual(ido["mirada"]["fuente"], "emitido")
        self.assertEqual(dec["informe"]["estado"], "sin_informe")
        self.assertEqual(dec["mirada"]["fuente"], "vigente")
        self.assertNotIn("ia", ido)

    def test_staff_also_sees_the_ai_state(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.URL, {"organization": "caf"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        ido = next(e for e in response.data[0]["etapas"] if e["stage"] == "ido_cci")
        self.assertEqual(ido["ia"]["estado"], ExecutionStatus.COMPLETED)


class LanzamientoTestCase(_Base):
    def test_launch_stage_workflows_passes_the_inherited_steps(self):
        # El principal es el IDO: el DEC espera su documento.
        link = ProjectDocument.objects.get(project=self.project, document=self.blueprint)
        link.tags.set(EvidenceTag.objects.filter(slug="ido"))
        link.tags_overridden = True
        link.save(update_fields=["tags_overridden"])
        self._emitir_ido()
        response = self._a_dec(generar=True)
        self.assertEqual(response.data["launches"][0]["status"], "blocked")
        doc_dec = Document.objects.create(
            owner=self.owner, name="Documento DEC", slug="doc-dec", chunking_status="done", extracted_text="x",
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc_dec, tags_overridden=True)
        link.tags.set(EvidenceTag.objects.filter(slug="dec"))
        Document.objects.filter(source__startswith="informe-etapa:").update(chunking_status="done")
        self.project.blueprint_document = doc_dec
        self.project.save(update_fields=["blueprint_document"])
        with patch(DELAY), patch(DOC_DISPATCH), self.captureOnCommitCallbacks(execute=True):
            results = launch_stage_workflows(self.project, "dec", self.owner)
        self.assertEqual(results[0]["status"], "launched", results)
        run = SkillExecution.objects.get(project=self.project, skill=self.dec)
        self.assertEqual(run.steps_completed, 2)
        self.assertTrue(all("heredado_de" in s for s in run.output_structured["steps"]))


class GuiadoConLaNumeracionDelDecTestCase(_Base):
    """El guiado escribe sobre las secciones del informe de la etapa, por su contenido."""

    DEC_STEPS = [
        "B.1. Marco de políticas",
        "A.3. Misión de Evaluación",
        "C.1. Evaluación de los co-beneficios ambientales, climáticos y de biodiversidad",
        "D.1.1. Criterio Técnico 1 (CT A1): Análisis de riesgos climáticos",
        "D.1.4. Determinación de Alineación con el Objetivo 1: Desarrollo Resiliente al Clima",
        "D.2.6. Determinación de Alineación con el Objetivo 2: Desarrollo bajo en emisiones de GEI",
        "E.1. Recomendaciones para fortalecer la Alineación con el Acuerdo de París",
        "E.2. Concepto de la DATBC",
    ]

    def _dec_run(self):
        self.project.context_notes = {**self.project.context_notes, "estado": "dec"}
        self.project.save(update_fields=["context_notes"])
        return SkillExecution.objects.create(
            skill=self.dec, owner=self.owner, project=self.project, status=ExecutionStatus.COMPLETED,
            stage="dec", finished_at=timezone.now(),
            output_structured={"steps": [
                {"step_id": 700 + i, "title": t, "content": f"Texto de Ecofilia {i}."}
                for i, t in enumerate(self.DEC_STEPS)
            ]},
        )

    def test_the_sections_are_the_dec_report_ones(self):
        from apps.project.services.stage_reports import guided_section_titles

        self._dec_run()
        self.assertEqual(guided_section_titles(self.project), self.DEC_STEPS[2:])

    def test_in_ido_they_are_the_ido_report_ones(self):
        from apps.project.services.stage_reports import guided_section_titles

        # La corrida IDO del armado: sólo «C.1. Co-beneficios» es del ejecutivo.
        self.assertEqual(guided_section_titles(self.project), ["C.1. Co-beneficios"])

    def test_the_determination_goes_to_d14_and_not_to_the_recommendations(self):
        from apps.project.services.stage_reports import generate_guiado

        run = self._dec_run()
        self._determinar()
        generate_guiado(self.project, self.editor, [
            {"titulo": self.DEC_STEPS[4], "contenido": "Determinación del ejecutivo, Objetivo 1."},
            {"titulo": self.DEC_STEPS[6], "contenido": "Recomendaciones del ejecutivo."},
        ])
        publicado = {
            e.step_id: e.published for e in ExecutionSectionEdit.objects.filter(execution=run)
        }
        self.assertEqual(publicado[704], "Determinación del ejecutivo, Objetivo 1.")
        self.assertEqual(publicado[706], "Recomendaciones del ejecutivo.")
