"""
Un 500 deja su traceback en la salida de la API, que es lo que llega a
CloudWatch (ver ``LOGGING`` en ``main/settings/base.py``).
"""
import io
import logging
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.project.api.views import ProjectViewSet

User = get_user_model()


class ServerErrorLoggingTestCase(APITestCase):
    def test_unhandled_exception_logs_its_traceback(self):
        handlers = logging.getLogger("django.request").handlers
        self.assertTrue(handlers, "django.request no tiene a dónde escribir")
        stream = io.StringIO()
        previous = [handler.setStream(stream) for handler in handlers]
        try:
            user = User.objects.create_user(email="u@example.com", password="x")
            self.client.force_authenticate(user)
            self.client.raise_request_exception = False
            with patch.object(ProjectViewSet, "list", side_effect=RuntimeError("se rompió")):
                response = self.client.get("/api/projects/")
        finally:
            for handler, old in zip(handlers, previous):
                handler.setStream(old)
        self.assertEqual(response.status_code, 500)
        output = stream.getvalue()
        self.assertIn("Traceback", output)
        self.assertIn("RuntimeError: se rompió", output)
        self.assertIn("/api/projects/", output)

    def test_client_errors_stay_out_of_the_log(self):
        handlers = logging.getLogger("django.request").handlers
        stream = io.StringIO()
        previous = [handler.setStream(stream) for handler in handlers]
        try:
            response = self.client.get("/api/projects/no-existe/")
        finally:
            for handler, old in zip(handlers, previous):
                handler.setStream(old)
        self.assertIn(response.status_code, (401, 404))
        self.assertEqual(stream.getvalue(), "")
