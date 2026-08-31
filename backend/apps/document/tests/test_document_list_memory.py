"""
El listado de documentos no debe traer el texto completo de cada archivo.

El 31-ago-2026 los workers de gunicorn de la API murieron por SIGKILL en seis
tareas distintas y el balanceador se quedó sin destinos sanos. La causa: el
queryset del listado era ``Document.objects.all()``, y Django trae todos los
campos concretos salvo que se le diga lo contrario — incluido
``extracted_text``, que en esta base tiene documentos de más de un millón de
caracteres. Listar la biblioteca cargaba a memoria el texto íntegro de cada
documento sólo para descartarlo al serializar.

Lo que se protege acá no es el tamaño de la respuesta —eso se ve— sino el
consumo invisible: las columnas que viajan de Postgres al proceso.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.document.models import Document

User = get_user_model()


class DocumentListDefersHeavyColumnsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="duenio@example.com", password="secret123", username="duenio",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.doc = Document.objects.create(
            owner=self.user,
            name="Informe pesado",
            slug="informe-pesado",
            description="Descripción corta, sí se usa en el buscador del front.",
            content_summary="R" * 5_000,
            extracted_text="T" * 500_000,
        )

    def _url(self):
        return "/api/document/list/"

    def test_extracted_text_is_not_loaded_from_the_database(self):
        """La comprobación central: el campo queda diferido, no traído y
        descartado. Si alguien saca el `defer`, esto vuelve a fallar."""
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from apps.document.api.views import DocumentListAPIView

        # La vista lee `query_params`, que sólo existe en el Request de DRF —
        # el `wsgi_request` de la respuesta de test no lo tiene.
        raw = APIRequestFactory().get(self._url(), {"scope": "own"})
        raw.user = self.user
        view = DocumentListAPIView()
        view.request = Request(raw)
        view.request.user = self.user

        obj = view.get_queryset().first()
        # `get_deferred_fields` lista lo que la instancia NO trajo de la base.
        self.assertIn("extracted_text", obj.get_deferred_fields())
        self.assertIn("content_summary", obj.get_deferred_fields())

    def test_the_list_response_carries_no_content_summary(self):
        response = self.client.get(self._url(), {"scope": "own"})
        self.assertEqual(response.status_code, 200)
        fila = response.data[0]

        self.assertNotIn("content_summary", fila)
        self.assertNotIn("extracted_text", fila)

    def test_description_survives_because_the_frontend_searches_on_it(self):
        """Las dos bibliotecas del front filtran por `description` del lado del
        cliente. Sacarlo por error rompería la búsqueda en silencio."""
        response = self.client.get(self._url(), {"scope": "own"})
        self.assertIn("description", response.data[0])
        self.assertIn("buscador", response.data[0]["description"])

    def test_a_single_document_still_returns_its_summary(self):
        """El recorte es sólo del listado: el serializador que responde altas y
        ediciones de un documento suelto no cambió de forma."""
        from apps.document.api.serializers import DocumentSerializer

        self.assertIn("content_summary", DocumentSerializer.Meta.fields)

    def test_the_unpaginated_response_is_still_a_flat_list(self):
        """Hay clientes que dependen de esta forma. El techo no la cambia."""
        response = self.client.get(self._url(), {"scope": "own"})
        self.assertIsInstance(response.data, list)

    def test_the_paginated_path_keeps_working(self):
        response = self.client.get(
            self._url(), {"scope": "own", "paginate": "1", "page": 1}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)
