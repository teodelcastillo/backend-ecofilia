"""Historial de la determinación de alineación: la base real del IDO → DEC y la evolución."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.project.models import Project, ProjectAlignmentSnapshot

User = get_user_model()


class AlignmentHistoryTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="h@example.com", password="x", username="h")
        self.other = User.objects.create_user(email="o@example.com", password="x", username="o")
        self.client.force_authenticate(self.user)
        self.project = Project.objects.create(
            owner=self.user, name="Op", context_notes={"estado": "ido_cci", "monto": "50"}
        )
        self.url = reverse("project-detail", kwargs={"slug": self.project.slug})

    def _patch(self, **notes):
        current = Project.objects.get(pk=self.project.pk).context_notes
        return self.client.patch(self.url, {"context_notes": {**current, **notes}}, format="json")

    def test_saving_a_determination_takes_a_snapshot(self):
        self._patch(alineacion_paris={"objetivo_1": "Alineada", "actualizado_en": "2026-09-01"})
        snap = ProjectAlignmentSnapshot.objects.get()
        self.assertEqual(snap.estado, "ido_cci")
        self.assertEqual(snap.alineacion, {"objetivo_1": "Alineada"})
        self.assertEqual(snap.captured_by, self.user)

    def test_unchanged_determination_does_not_duplicate(self):
        self._patch(alineacion_paris={"objetivo_1": "Alineada", "actualizado_en": "a"})
        # Sólo cambian los metadatos de edición y otro campo de la operación.
        self._patch(alineacion_paris={"objetivo_1": "Alineada", "actualizado_en": "b"}, sector="Agua")
        self.assertEqual(ProjectAlignmentSnapshot.objects.count(), 1)

    def test_stage_change_takes_a_new_snapshot(self):
        self._patch(alineacion_paris={"objetivo_1": "Con brechas por CT A2"})
        self._patch(estado="dec", alineacion_paris={"objetivo_1": "Alineada"})
        estados = list(ProjectAlignmentSnapshot.objects.values_list("estado", flat=True))
        self.assertEqual(estados, ["ido_cci", "dec"])

    def test_no_determination_no_snapshot(self):
        self._patch(estado="dec")
        self.assertFalse(ProjectAlignmentSnapshot.objects.exists())

    def test_history_endpoint_only_lists_visible_operations(self):
        self._patch(alineacion_paris={"objetivo_1": "Alineada"})
        ajena = Project.objects.create(owner=self.other, name="Ajena")
        ProjectAlignmentSnapshot.objects.create(
            project=ajena, alineacion={"objetivo_1": "No alineada"}, captured_at="2026-01-01T00:00:00Z"
        )
        response = self.client.get(reverse("project-alignment-history"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([r["project_slug"] for r in response.data], [self.project.slug])


from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BackfillAlignmentSnapshotsMigrationTestCase(TransactionTestCase):
    BEFORE = [("project", "0013_alignment_snapshots")]
    AFTER = [("project", "0014_backfill_alignment_snapshots")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.BEFORE)
        apps = executor.loader.project_state(self.BEFORE).apps
        Project = apps.get_model("project", "Project")
        owner_id = User.objects.create_user(email="b@example.com", password="x", username="b").id
        Project.objects.create(
            owner_id=owner_id, name="Con", slug="con",
            context_notes={"estado": "dec", "alineacion_paris": {
                "objetivo_2": "Con brechas", "actualizado_en": "2026-03-15T10:00:00Z"}},
        )
        Project.objects.create(owner_id=owner_id, name="Sin", slug="sin", context_notes={"estado": "dec"})
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.AFTER)

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_one_backfill_snapshot_per_operation_with_determination(self):
        snaps = list(ProjectAlignmentSnapshot.objects.all())
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].project.slug, "con")
        self.assertEqual(snaps[0].source, "backfill")
        self.assertEqual(snaps[0].estado, "dec")
        self.assertEqual(snaps[0].alineacion, {"objetivo_2": "Con brechas"})
        self.assertEqual(snaps[0].captured_at.isoformat()[:10], "2026-03-15")
