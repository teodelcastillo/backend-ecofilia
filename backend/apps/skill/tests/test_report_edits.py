"""
Mesa de trabajo del informe: guardar, publicar y ascender.

Lo que se prueba es la promesa que sostiene la pantalla: escribir no mueve el
informe, publicar lo mueve entero y de una vez, y la salida del agente sigue
disponible pase lo que pase.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from rest_framework import status
from rest_framework.test import APITestCase

from apps.project.models import Project, ProjectShareRole
from apps.skill.models import (
    ExecutionSectionEdit,
    ExecutionStatus,
    Skill,
    SkillExecution,
    SkillType,
)

User = get_user_model()

STEPS = {
    "steps": [
        {"step_id": 1, "title": "B.1. Marco de políticas", "content": "Original B.1."},
        {"step_id": 2, "title": "B.2. Riesgos", "content": "Original B.2."},
    ]
}


class ReportEditsTestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="secret123", username="owner"
        )
        self.editor = User.objects.create_user(
            email="editor@example.com", password="secret123", username="editor"
        )
        self.outsider = User.objects.create_user(
            email="outsider@example.com", password="secret123", username="outsider"
        )
        self.skill = Skill.objects.create(
            name="IET",
            slug="iet-test",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            owner=self.owner,
        )
        self.project = Project.objects.create(owner=self.owner, name="Operación")
        self.project.shares.create(user=self.editor, role=ProjectShareRole.EDITOR)
        self.execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.owner,
            project=self.project,
            status=ExecutionStatus.COMPLETED,
            output_structured=STEPS,
            finished_at=timezone.now(),
        )
        self.client.force_authenticate(self.owner)

    def _section_url(self, step_id, suffix=""):
        base = reverse(
            "skill-execution-detail", kwargs={"pk": self.execution.pk}
        ).rstrip("/")
        return f"{base}/sections/{step_id}/{suffix}"

    def _publish_url(self):
        base = reverse(
            "skill-execution-detail", kwargs={"pk": self.execution.pk}
        ).rstrip("/")
        return f"{base}/publish/"

    # ── Guardar ──────────────────────────────────────────────────────────

    def test_saving_a_draft_does_not_touch_the_report(self):
        response = self.client.put(
            self._section_url(1), {"content": "Reescrito"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["draft"], "Reescrito")
        self.assertEqual(response.data["published"], "")
        self.assertTrue(response.data["has_unpublished_changes"])

    def test_a_step_the_run_never_wrote_is_rejected(self):
        response = self.client.put(
            self._section_url(99), {"content": "Inventado"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_original_output_is_never_modified(self):
        self.client.put(self._section_url(1), {"content": "Reescrito"}, format="json")
        self.client.post(self._publish_url(), {}, format="json")

        self.execution.refresh_from_db()
        self.assertEqual(
            self.execution.output_structured["steps"][0]["content"], "Original B.1."
        )

    # ── Publicar ─────────────────────────────────────────────────────────

    def test_publishing_moves_every_pending_draft_at_once(self):
        self.client.put(self._section_url(1), {"content": "Nuevo B.1"}, format="json")
        self.client.put(self._section_url(2), {"content": "Nuevo B.2"}, format="json")

        response = self.client.post(self._publish_url(), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["published"]), 2)
        self.assertEqual(
            {e["published"] for e in response.data["published"]},
            {"Nuevo B.1", "Nuevo B.2"},
        )
        self.assertEqual(response.data["execution"]["published_sections_count"], 2)
        self.assertEqual(response.data["execution"]["unpublished_sections_count"], 0)

    def test_publishing_with_nothing_pending_writes_no_history(self):
        self.client.put(self._section_url(1), {"content": "Nuevo"}, format="json")
        self.client.post(self._publish_url(), {}, format="json")

        response = self.client.post(self._publish_url(), {}, format="json")

        self.assertEqual(response.data["published"], [])
        edit = ExecutionSectionEdit.objects.get(execution=self.execution, step_id=1)
        self.assertEqual(edit.versions.count(), 1)

    def test_each_publication_leaves_a_version_to_come_back_to(self):
        self.client.put(self._section_url(1), {"content": "Primera"}, format="json")
        self.client.post(self._publish_url(), {}, format="json")
        self.client.put(self._section_url(1), {"content": "Segunda"}, format="json")
        self.client.post(self._publish_url(), {}, format="json")

        versions = self.client.get(self._section_url(1, "versions/")).data
        self.assertEqual([v["version_number"] for v in versions], [2, 1])

        restored = self.client.post(self._section_url(1, "versions/1/restore/"))
        # Restaurar deja el texto en el borrador: publicar sigue siendo un acto
        # aparte, así nadie vuelve atrás el informe sin querer.
        self.assertEqual(restored.data["draft"], "Primera")
        self.assertEqual(restored.data["published"], "Segunda")

    # ── Descartar ────────────────────────────────────────────────────────

    def test_discarding_a_draft_falls_back_to_what_was_published(self):
        self.client.put(self._section_url(1), {"content": "Publicado"}, format="json")
        self.client.post(self._publish_url(), {}, format="json")
        self.client.put(self._section_url(1), {"content": "A medio hacer"}, format="json")

        response = self.client.post(self._section_url(1, "discard/"))

        self.assertEqual(response.data["draft"], "")
        self.assertEqual(response.data["published"], "Publicado")
        self.assertFalse(response.data["has_unpublished_changes"])

    # ── Permisos ─────────────────────────────────────────────────────────

    def test_a_project_editor_can_work_the_report_without_owning_the_run(self):
        self.client.force_authenticate(self.editor)

        response = self.client.put(
            self._section_url(1), {"content": "Del ejecutivo"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_someone_outside_the_project_cannot_edit(self):
        self.client.force_authenticate(self.outsider)

        response = self.client.put(
            self._section_url(1), {"content": "Ajeno"}, format="json"
        )

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    # ── Ascender una corrida ─────────────────────────────────────────────

    def test_promoting_an_old_run_puts_it_ahead_of_a_newer_one(self):
        # Las dos en el pasado: una corrida no puede haber terminado después
        # del momento en que alguien asciende la otra.
        self.execution.finished_at = timezone.now() - timedelta(hours=2)
        self.execution.save(update_fields=["finished_at"])
        newer = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.owner,
            project=self.project,
            status=ExecutionStatus.COMPLETED,
            output_structured=STEPS,
            finished_at=timezone.now() - timedelta(hours=1),
        )
        base = reverse("skill-execution-detail", kwargs={"pk": self.execution.pk})
        response = self.client.post(f"{base.rstrip('/')}/promote/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.execution.refresh_from_db()
        self.assertIsNotNone(self.execution.promoted_at)
        # La vigente es la fecha más nueva entre terminar y ascender: la vieja
        # ascendida le gana a la nueva hasta que corra otra.
        self.assertGreater(self.execution.promoted_at, newer.finished_at)
