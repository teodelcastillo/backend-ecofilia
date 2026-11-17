"""Un doble de ``retrieve_for_chat`` para los tests del motor.

Hasta junio de 2026 skills, copilot, research y evaluaciones llamaban a
``fetch_relevant_chunks`` y los tests parcheaban esa función para inyectar
fragmentos. Desde ``ef8eb92`` todos pasan por ``retrieve_for_chat``, que llama a
``fetch_relevant_chunks`` desde ``apps.chat.services.rag``: parchear el nombre
importado en ``apps.skill.services`` dejó de tener efecto, y los tests corrían
la recuperación real (con embeddings de verdad, si había clave).

Este mock se parchea donde el código llama hoy::

    @patch("apps.skill.services.retrieve_for_chat", new_callable=RetrievalMock)

y conserva la forma de los tests viejos: ``return_value`` o ``side_effect``
pueden devolver una lista de fragmentos, que llega al código envuelta en un
``RetrievalResult``. ``call_args`` y ``call_count`` se leen igual que antes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from apps.chat.services.rag import RetrievalResult


class RetrievalMock(MagicMock):
    def __call__(self, *args, **kwargs):
        result = super().__call__(*args, **kwargs)
        if isinstance(result, list):
            return RetrievalResult(chunks=result)
        return result
