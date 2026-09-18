"""Tests de la red de seguridad para documentos trabados en `pending`."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from apps.document.models import ChunkingStatus, Document
from apps.document.reliability import requeue_stuck_documents

User = get_user_model()


def _make_document(owner, **overrides) -> Document:
    with patch("apps.document.signals.dispatch_processing"):
        doc = Document.objects.create(
            owner=owner,
            name=overrides.pop("name", "Informe ESG"),
            file=overrides.pop(
                "file", SimpleUploadedFile("informe.pdf", b"%PDF-1.4 fake")
            ),
        )
    if overrides:
        Document.objects.filter(pk=doc.pk).update(**overrides)
        doc.refresh_from_db()
    return doc


class RequeueStuckDocumentsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="pass1234"
        )

    def _stuck(self, minutes: int = 60, **overrides) -> Document:
        overrides.setdefault(
            "status_changed_at", timezone.now() - timedelta(minutes=minutes)
        )
        return _make_document(
            self.user, chunking_status=ChunkingStatus.PENDING, **overrides
        )

    def test_requeues_document_pending_past_the_threshold(self):
        doc = self._stuck()

        with patch("apps.document.reliability.dispatch_processing", return_value=True) as dispatch:
            result = requeue_stuck_documents()

        dispatch.assert_called_once_with(doc.pk)
        self.assertEqual(result["requeued"], [doc.pk])

        doc.refresh_from_db()
        # Reencolar no cambia el estado visible: sigue pendiente hasta que un
        # worker lo reclame. Lo que cambia es el sello y el contador.
        self.assertEqual(doc.chunking_status, ChunkingStatus.PENDING)
        self.assertEqual(doc.requeue_count, 1)

    def test_leaves_recently_queued_documents_alone(self):
        self._stuck(minutes=1)

        with patch("apps.document.reliability.dispatch_processing") as dispatch:
            result = requeue_stuck_documents()

        dispatch.assert_not_called()
        self.assertEqual(result["requeued"], [])

    def test_resets_the_clock_so_the_next_pass_does_not_requeue_again(self):
        """Sin el sello, el reaper reenviaría el mismo documento cada 5 minutos."""
        self._stuck()

        with patch("apps.document.reliability.dispatch_processing", return_value=True):
            requeue_stuck_documents()
            second = requeue_stuck_documents()

        self.assertEqual(second["requeued"], [])

    def test_falls_back_to_created_at_when_the_stamp_is_missing(self):
        """Los documentos ya trabados no tienen sello: son los que hay que rescatar."""
        doc = self._stuck(status_changed_at=None)
        Document.objects.filter(pk=doc.pk).update(
            created_at=timezone.now() - timedelta(hours=3)
        )

        with patch("apps.document.reliability.dispatch_processing", return_value=True) as dispatch:
            requeue_stuck_documents()

        dispatch.assert_called_once_with(doc.pk)

    def test_gives_up_after_the_maximum_number_of_requeues(self):
        doc = self._stuck(requeue_count=3)

        with patch("apps.document.reliability.dispatch_processing") as dispatch:
            result = requeue_stuck_documents()

        dispatch.assert_not_called()
        self.assertEqual(result["exhausted"], [doc.pk])
        doc.refresh_from_db()
        self.assertEqual(doc.chunking_status, ChunkingStatus.ERROR)
        self.assertIn("reenvíos automáticos", doc.last_error)

    def test_marks_error_when_the_document_has_no_file(self):
        doc = self._stuck(file="")

        with patch("apps.document.reliability.dispatch_processing") as dispatch:
            result = requeue_stuck_documents()

        dispatch.assert_not_called()
        self.assertEqual(result["failed"], [doc.pk])
        doc.refresh_from_db()
        self.assertEqual(doc.chunking_status, ChunkingStatus.ERROR)

    def test_ignores_documents_that_are_not_pending(self):
        for status in (
            ChunkingStatus.PROCESSING,
            ChunkingStatus.DONE,
            ChunkingStatus.PARTIAL,
            ChunkingStatus.ERROR,
        ):
            _make_document(
                self.user,
                chunking_status=status,
                status_changed_at=timezone.now() - timedelta(hours=5),
            )

        with patch("apps.document.reliability.dispatch_processing") as dispatch:
            requeue_stuck_documents()

        dispatch.assert_not_called()


class DispatchProcessingTests(TestCase):
    """Un encolado que falla tiene que dejar rastro, no un PENDING eterno."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="dispatch@example.com", password="pass1234"
        )

    def test_marks_error_when_the_broker_rejects_the_message(self):
        from apps.document.dispatch import dispatch_processing

        doc = _make_document(self.user)

        with patch(
            "apps.document.dispatch.process_document_chunks.delay",
            side_effect=RuntimeError("SQS unavailable"),
        ):
            ok = dispatch_processing(doc.pk)

        self.assertFalse(ok)
        doc.refresh_from_db()
        self.assertEqual(doc.chunking_status, ChunkingStatus.ERROR)
        self.assertIn("SQS unavailable", doc.last_error)

    def test_returns_true_and_leaves_the_document_alone_on_success(self):
        from apps.document.dispatch import dispatch_processing

        doc = _make_document(self.user, chunking_status=ChunkingStatus.PENDING)

        with patch("apps.document.dispatch.process_document_chunks.delay") as delay:
            ok = dispatch_processing(doc.pk)

        self.assertTrue(ok)
        delay.assert_called_once_with(doc.pk)
        doc.refresh_from_db()
        self.assertEqual(doc.chunking_status, ChunkingStatus.PENDING)
