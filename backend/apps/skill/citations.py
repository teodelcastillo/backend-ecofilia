"""
De la cita que devuelve la API a la referencia que sirve para verificar.

La API devuelve, por cada afirmación citada, el fragmento literal y dónde
estaba: sobre texto plano, un rango de caracteres del bloque que le mandamos.
Eso todavía no es trazabilidad. Quien revisa un IET en CAF abre el PDF y busca
la página; un offset de 213.480 caracteres no le dice nada.

Este módulo hace las dos traducciones que faltan:

**De offset a página**, usando el mapa que ``apps.document.page_map`` armó al
sacar los marcadores del texto. Un documento sin marcadores —los procesados con
el método viejo— queda con offset y sin página, declarado como tal. No se
estima: una referencia que manda al lector a la página equivocada es peor que
una que admite no saber, porque la primera se descubre recién cuando alguien
va a buscarla y no la encuentra.

**De cita a cita verificada.** La API devuelve el texto literal, así que se
puede comprobar que ese texto está de verdad donde dice estar. Eso convierte
"el modelo dijo que lo dice el NAP" en algo que la máquina chequea sola, y da
la métrica que le faltaba al producto: qué porcentaje de las afirmaciones
citadas resiste la verificación.
"""
from __future__ import annotations

import logging

from apps.document.page_map import format_reference

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Colapsa espacios para comparar textos que sólo difieren en formato."""
    return " ".join((text or "").split())


def _verify(payload, start: int | None, end: int | None, cited_text: str) -> bool:
    """¿El texto citado está realmente donde la cita dice que está?

    Se compara primero literal y después con los espacios colapsados: un salto
    de línea de más en el recorte no es una cita falsa, y contarla como tal
    haría inservible la métrica.
    """
    if start is None or end is None or not cited_text:
        return False
    excerpt = payload.text[start:end]
    if excerpt == cited_text:
        return True
    return _normalize(excerpt) == _normalize(cited_text)


def resolve_citations(citations: list[dict], payloads: list) -> list[dict]:
    """Convierte las citas crudas de la API en fuentes con página.

    ``document_index`` es la posición del documento entre los bloques
    ``document`` del pedido, así que ``payloads`` tiene que venir en el mismo
    orden en que se armaron los bloques. Si no coincidiera, una cita quedaría
    atribuida al documento equivocado — por eso el índice fuera de rango se
    descarta y se registra, en vez de caer al primero.
    """
    resolved: list[dict] = []
    for citation in citations or []:
        index = citation.get("document_index")
        if not isinstance(index, int) or not (0 <= index < len(payloads)):
            logger.warning(
                "Cita con document_index fuera de rango (%s de %d documentos); se descarta.",
                index,
                len(payloads),
            )
            continue
        payload = payloads[index]

        start = citation.get("start_char_index")
        end = citation.get("end_char_index")
        page_start = citation.get("start_page_number")
        page_end = citation.get("end_page_number")
        if page_start is None and start is not None:
            page_start, page_end = payload.page_map.page_range(start, end or start + 1)

        resolved.append(
            {
                "document_slug": payload.slug,
                "document_name": payload.name,
                # La interfaz resuelve el visor por (slug, chunk_index); una
                # cita no viene de un fragmento, así que va en None y se
                # distingue por `delivery`. Sin la clave, el dedup global la
                # trataría como una fuente distinta en cada paso.
                "chunk_index": None,
                "delivery": "citation",
                "page_start": page_start,
                "page_end": page_end,
                "reference": format_reference(page_start, page_end),
                "cited_text": citation.get("cited_text", ""),
                "char_start": start,
                "char_end": end,
                "verified": _verify(payload, start, end, citation.get("cited_text", "")),
            }
        )
    return resolved


def citation_stats(resolved: list[dict]) -> dict:
    """Lo que se persiste por paso sobre la calidad de las citas.

    ``with_page`` importa tanto como ``verified``: una cita verificada sobre un
    documento sin marcadores de página es cierta pero no ubicable, y el informe
    no debería presentarla igual que una que manda a la página 47.
    """
    total = len(resolved)
    if not total:
        return {"citations": 0}
    verified = sum(1 for c in resolved if c["verified"])
    with_page = sum(1 for c in resolved if c["page_start"] is not None)
    documents = sorted({c["document_slug"] for c in resolved})
    return {
        "citations": total,
        "citations_verified": verified,
        "citations_verified_ratio": round(verified / total, 4),
        "citations_with_page": with_page,
        "citations_documents": documents,
    }
