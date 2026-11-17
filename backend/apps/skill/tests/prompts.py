"""Leer en los tests lo que el motor le mandó al modelo.

Un paso de copilot ya no manda el prompt como un string: con contexto-primero
el mensaje del usuario es una lista de bloques (inventario y documentos con su
punto de caché, y al final el prompt del paso). Buscar un texto con
``assertIn(texto, mensaje["content"])`` sobre esa lista compara contra los
bloques enteros y nunca encuentra nada.
"""
from __future__ import annotations


def user_prompt(messages: list[dict]) -> str:
    """El texto completo del mensaje del usuario, sea string o lista de bloques."""
    content = messages[1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )
