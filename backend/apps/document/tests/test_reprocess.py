"""Tests del endpoint de reprocesamiento manual."""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import ChunkingStatus, Document

User = get_user_model()


class ReprocessDocumentTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="pass1234"
        )
        self.other = User.objects.create_user(
            email="other@example.com", password="pass1234"
        )
        # El signal post_save encola la ingesta al crear; acá no interesa.
        with patch("apps.document.signals.process_document_chunks"):
            self.document = Document.objects.create(
                owner=self.owner,
                name="Código urbano",
                file=SimpleUploadedFile("codigo.pdf", b"%PDF-1.4 fake"),
                chunking_status=ChunkingStatus.ERROR,
                last_error="El documento tiene 12 páginas, todas escaneadas.",
            )

    def _url(self):
        return reverse("document-reprocess", kwargs={"slug": self.document.slug})

    def test_owner_can_reprocess(self):
        self.client.force_authenticate(self.owner)
        with patch("apps.document.api.views.process_document_chunks") as task:
            response = self.client.post(self._url())

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        task.delay.assert_called_once_with(self.document.pk)

        self.document.refresh_from_db()
        self.assertEqual(self.document.chunking_status, ChunkingStatus.PENDING)
        self.assertFalse(self.document.chunking_done)
        self.assertEqual(self.document.last_error, "")
        self.assertEqual(self.document.retry_count, 1)

    def test_status_is_reset_so_the_task_guard_lets_it_through(self):
        """El estado debe salir de DONE o la tarea aborta sin hacer nada."""
        Document.objects.filter(pk=self.document.pk).update(
            chunking_status=ChunkingStatus.DONE, chunking_done=True
        )
        self.client.force_authenticate(self.owner)
        with patch("apps.document.api.views.process_document_chunks"):
            self.client.post(self._url())

        self.document.refresh_from_db()
        self.assertEqual(self.document.chunking_status, ChunkingStatus.PENDING)

    def test_stranger_cannot_reprocess(self):
        self.client.force_authenticate(self.other)
        with patch("apps.document.api.views.process_document_chunks") as task:
            response = self.client.post(self._url())

        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        task.delay.assert_not_called()

    def test_anonymous_is_rejected(self):
        response = self.client.post(self._url())
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_document_being_processed_is_not_disturbed(self):
        """Dos corridas sobre los mismos chunks chocan contra el unique."""
        Document.objects.filter(pk=self.document.pk).update(
            chunking_status=ChunkingStatus.PROCESSING
        )
        self.client.force_authenticate(self.owner)
        with patch("apps.document.api.views.process_document_chunks") as task:
            response = self.client.post(self._url())

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        task.delay.assert_not_called()
