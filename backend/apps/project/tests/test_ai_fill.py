"""
Extracción de objetivo y componentes desde el documento principal.

Los casos salen de fallas reales de producción (septiembre 2026): el modelo
seguía escribiendo el documento cortado en vez de responder, o envolvía el JSON
en negritas; y una operación creada con su documento todavía en proceso
quedaba sin objetivo ni componentes para siempre.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.document.models import Document
from apps.project.models import Project
from apps.project.services import ai_fill

User = get_user_model()

STRUCTURED = "apps.project.services.ai_fill.anthropic_structured_completion"
OPENAI = "apps.project.services.ai_fill.generate_chat_completion"
MODEL = "apps.project.services.ai_fill.effective_chat_model"


class ExtractFieldsTestCase(SimpleTestCase):
    def test_document_goes_first_and_instructions_last(self):
        """Con el documento al final, cortado, el modelo lo continuaba."""
        with patch(MODEL, return_value="claude-sonnet-5"), patch(
            STRUCTURED, return_value=({"objetivo": "Financiar X"}, {})
        ) as call:
            ai_fill.extract_fields("TEXTO DEL IDO", ["objetivo"])
        user = call.call_args.args[0][-1]["content"]
        self.assertTrue(user.startswith("<documento>\nTEXTO DEL IDO\n</documento>"))
        self.assertIn("extraé estos campos", user.split("</documento>")[1])
        self.assertEqual(
            call.call_args.kwargs["schema"]["required"], ["objetivo"]
        )

    def test_long_document_keeps_the_sections_that_matter(self):
        """La DESCRIPCIÓN de un IDO suele estar lejos del comienzo."""
        relleno = "antecedentes del sector. " * 40_000  # ~1M caracteres
        texto = relleno + "DESCRIPCIÓN DE LA OPERACIÓN: el componente 1 financia obras." + relleno
        with patch.object(ai_fill, "_MAX_DOC_CHARS", 200_000):
            elegido = ai_fill._select_text(texto)
        self.assertLessEqual(len(elegido), 200_000 + 500)
        self.assertIn("el componente 1 financia obras", elegido)
        self.assertIn("se omite una parte", elegido)

    def test_short_document_goes_whole(self):
        self.assertEqual(ai_fill._select_text("corto"), "corto")

    def test_retries_once_after_a_failure(self):
        with patch(MODEL, return_value="claude-sonnet-5"), patch(
            STRUCTURED,
            side_effect=[ValueError("El modelo respondió sin texto."), ({"objetivo": "Ok"}, {})],
        ):
            result = ai_fill.extract_fields("texto", ["objetivo"])
        self.assertEqual(result, {"objetivo": "Ok"})

    def test_two_failures_raise(self):
        with patch(MODEL, return_value="claude-sonnet-5"), patch(
            STRUCTURED, side_effect=ValueError("x")
        ):
            with self.assertRaises(RuntimeError):
                ai_fill.extract_fields("texto", ["objetivo"])

    def test_blank_value_is_none(self):
        with patch(MODEL, return_value="claude-sonnet-5"), patch(
            STRUCTURED, return_value=({"objetivo": "  ", "componentes": "1. A"}, {})
        ):
            result = ai_fill.extract_fields("texto", ["objetivo", "componentes"])
        self.assertEqual(result, {"objetivo": None, "componentes": "1. A"})

    def test_openai_path_tolerates_wrapped_json(self):
        """El caso de Colombia: la respuesta empezaba con ``**{``."""
        with patch(MODEL, return_value="gpt-4o-mini"), patch(
            OPENAI, return_value=('**{"componentes": ["1. A", "2. B"]}**', {})
        ):
            result = ai_fill.extract_fields("texto", ["componentes"])
        self.assertEqual(result, {"componentes": "1. A\n2. B"})


class FillMissingFromBlueprintTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="ejec@example.com", password="secret123", username="ejec"
        )
        self.ido = Document.objects.create(
            owner=self.user, name="IDO", slug="ido", extracted_text="texto del IDO",
            chunking_status="done",
        )
        self.project = Project.objects.create(
            owner=self.user, name="Op", blueprint_document=self.ido,
            context_notes={"pais": "Chile", "objetivo": "Escrito a mano"},
        )

    def test_fills_only_what_is_missing(self):
        with patch.object(
            ai_fill, "extract_fields", return_value={"componentes": "1. Obra"}
        ) as extract:
            filled = ai_fill.fill_missing_from_blueprint(self.project.id)
        extract.assert_called_once_with("texto del IDO", ["componentes"])
        self.project.refresh_from_db()
        self.assertEqual(filled, {"componentes": "1. Obra"})
        self.assertEqual(self.project.context_notes["objetivo"], "Escrito a mano")
        self.assertEqual(self.project.context_notes["pais"], "Chile")

    def test_does_not_overwrite_what_someone_wrote_meanwhile(self):
        def escribe_mientras(*_args, **_kwargs):
            Project.objects.filter(pk=self.project.pk).update(
                context_notes={"objetivo": "Escrito a mano", "componentes": "1. A mano"}
            )
            return {"componentes": "1. Del modelo"}

        with patch.object(ai_fill, "extract_fields", side_effect=escribe_mientras):
            ai_fill.fill_missing_from_blueprint(self.project.id)
        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["componentes"], "1. A mano")

    def test_nothing_missing_is_a_noop(self):
        Project.objects.filter(pk=self.project.pk).update(
            context_notes={"objetivo": "a", "componentes": "b"}
        )
        with patch.object(ai_fill, "extract_fields") as extract:
            self.assertEqual(ai_fill.fill_missing_from_blueprint(self.project.id), {})
        extract.assert_not_called()

    def test_processing_document_gives_a_clear_message(self):
        Document.objects.filter(pk=self.ido.pk).update(extracted_text="", chunking_status="processing")
        self.project.refresh_from_db()
        with self.assertRaisesRegex(ValueError, "se completan solos"):
            ai_fill.ai_fill_project(self.project, ["objetivo"])

    def test_ingestion_end_dispatches_the_fill(self):
        from apps.project.tasks import dispatch_fill_for_blueprint

        with patch("apps.project.tasks.fill_from_blueprint_task.delay") as delay:
            dispatch_fill_for_blueprint(self.ido.id)
        delay.assert_called_once_with(self.project.id)


class AiFillSaveApiTestCase(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(email="s@example.com", password="x", username="s")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        ido = Document.objects.create(
            owner=self.user, name="IDO", slug="ido-s", extracted_text="t", chunking_status="done"
        )
        self.project = Project.objects.create(
            owner=self.user, name="Op", blueprint_document=ido, context_notes={"pais": "Chile"}
        )

    def test_save_merges_into_the_current_notes(self):
        """El Resumen mezclaba con una copia vieja y pisaba cambios ajenos."""
        from django.urls import reverse

        def cambia_mientras(*_a, **_k):
            Project.objects.filter(pk=self.project.pk).update(
                context_notes={"pais": "Chile", "monto": "50"}
            )
            return {"objetivo": "Financiar"}

        with patch.object(ai_fill, "extract_fields", side_effect=cambia_mientras):
            response = self.client.post(
                reverse("project-ai-fill", kwargs={"slug": self.project.slug}),
                {"fields": ["objetivo"], "save": True},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.context_notes, {"pais": "Chile", "monto": "50", "objetivo": "Financiar"}
        )
