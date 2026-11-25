"""La entrada de un embedding nunca pasa el tope del modelo.

El paso C.1 del DEC buscaba con su instrucción entera —trae el catálogo de
actividades— y OpenAI la rechazaba («maximum context length is 8192 tokens»):
la búsqueda fallaba y el paso se quedaba sin fragmentos de los documentos
degradados.
"""
from unittest import mock

from django.test import SimpleTestCase

from apps.document.utils import client_openia


class EmbeddingInputTests(SimpleTestCase):
    def _sent(self, text):
        fake = mock.Mock()
        fake.embeddings.create.return_value = mock.Mock(data=[mock.Mock(embedding=[0.1])])
        with mock.patch.object(client_openia, "get_openai_client", return_value=fake):
            client_openia.embed_text(text)
        return fake.embeddings.create.call_args.kwargs["input"]

    def test_a_long_query_is_cut_to_the_model_limit(self):
        catalogo = "M.11.7.1. Servicios técnicos para proyectos de mitigación. " * 2000
        sent = self._sent(catalogo)
        self.assertEqual(len(sent), client_openia.EMBEDDING_MAX_INPUT_CHARS)
        self.assertTrue(catalogo.startswith(sent))

    def test_a_normal_text_travels_untouched(self):
        texto = "Evaluación de los co-beneficios ambientales de la operación."
        self.assertEqual(self._sent(texto), texto)
