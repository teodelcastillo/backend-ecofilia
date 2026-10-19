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
                # Dónde, dentro del texto que escribió el modelo, está la
                # afirmación que esta cita sostiene. `char_*` apunta al
                # documento fuente y `content_*` al informe: son los dos
                # extremos del vínculo, y sin el segundo una cita sólo se puede
                # mostrar al pie de la sección y no junto a lo que sostiene.
                "content_start": citation.get("content_start"),
                "content_end": citation.get("content_end"),
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


# ---------------------------------------------------------------------------
# Citas dentro de las celdas de una tabla
# ---------------------------------------------------------------------------
#
# En un paso de texto la API devuelve las citas como bloques aparte y el motor
# las ancla al informe. En un paso de tabla el modelo responde JSON, y la cita
# no llega como cita: el modelo escribe adentro de la celda su marca interna,
# ``<cite index="3-196">afirmación</cite>`` (documento 3, oración 196). Nadie
# la resolvía, y el ejecutivo veía la etiqueta cruda.
#
# Se resuelve acá: se busca en ese documento el pasaje que mejor respalda la
# afirmación, se toma su página, y la celda queda con el texto y una marca
# ``[[c:N]]`` que la interfaz dibuja como chip (N es la posición de la cita en
# las citas del paso). La cita queda **verificada** sólo si el texto de la
# afirmación está literal en el documento; si se ubicó por parecido, va como
# no verificada, que es lo que es.

import re as _re

# Con "<" o "(": así llegó también, en producción, la marca de apertura.
CITE_TAG = _re.compile(r'[<(]cite\s+index="(\d+)-(\d+)[^"]*">(.*?)</cite>', _re.DOTALL)
_STRAY_TAG = _re.compile(r"[<(]/?cite[^>]*>")
CELL_MARKER = _re.compile(r"\[\[c:(\d+)\]\]")
_SENTENCE_END = _re.compile(r"(?<=[.!?;:])\s+|\n+")
_WORD = _re.compile(r"[\wáéíóúñü]{4,}", _re.IGNORECASE)
# Por debajo de esta proporción de palabras de la afirmación presentes en el
# pasaje, no se lo atribuye: se cita el documento sin página.
_MIN_OVERLAP = 0.35


def strip_cell_citations(text: str) -> str:
    """El texto de una celda sin marcas de cita (para prompts y comparaciones)."""
    text = CITE_TAG.sub(lambda m: m.group(3), text or "")
    text = _STRAY_TAG.sub("", text)
    return CELL_MARKER.sub("", text)


def _sentences(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if match.start() > start:
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")}


def _locate(payload, claim: str, hint: int) -> tuple[int, int, bool] | None:
    """El pasaje del documento que respalda ``claim``: (inicio, fin, literal).

    Si la afirmación está literal en el documento, es ese rango. Si no, la
    oración con más palabras en común, prefiriendo las cercanas al índice que
    dio el modelo cuando empatan.
    """
    text = payload.text or ""
    claim_clean = _normalize(claim)
    if len(claim_clean) >= 12:
        position = text.find(claim_clean)
        if position != -1:
            return position, position + len(claim_clean), True

    claim_words = _words(claim)
    if not claim_words:
        return None
    best: tuple[float, int, int, int] | None = None
    for index, (start, end) in enumerate(_sentences(text)):
        overlap = len(claim_words & _words(text[start:end])) / len(claim_words)
        if overlap <= 0:
            continue
        key = (overlap, -abs(index - hint), start, end)
        if best is None or key > best:
            best = key
    if best is None or best[0] < _MIN_OVERLAP:
        return None
    _, _, start, end = best
    literal = claim_clean in _normalize(text[start:end])
    return start, end, literal


def resolve_cell_citations(table: dict, payloads: list, offset: int) -> tuple[dict, list[dict]]:
    """Resuelve las citas escritas dentro de las celdas de ``table``.

    Devuelve la tabla con las celdas reescritas y las citas nuevas, que se
    agregan a las del paso a partir de la posición ``offset``.
    """
    found: list[dict] = []

    def _replace(match) -> str:
        doc_index, hint, claim = int(match.group(1)), int(match.group(2)), match.group(3)
        if not (0 <= doc_index < len(payloads)):
            return claim
        payload = payloads[doc_index]
        located = _locate(payload, claim, hint)
        if located is None:
            start = end = None
            page_start = page_end = None
            cited, verified = "", False
        else:
            start, end, verified = located
            page_start, page_end = payload.page_map.page_range(start, end)
            cited = payload.text[start:end].strip()[:600]
        found.append(
            {
                "document_slug": payload.slug,
                "document_name": payload.name,
                "chunk_index": None,
                "delivery": "citation",
                "page_start": page_start,
                "page_end": page_end,
                "reference": format_reference(page_start, page_end),
                "cited_text": cited,
                "char_start": start,
                "char_end": end,
                "content_start": None,
                "content_end": None,
                "verified": verified,
                "in_cell": True,
            }
        )
        return f"{claim}[[c:{offset + len(found) - 1}]]"

    rows = []
    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            rows.append(row)
            continue
        new_row = {}
        for key, value in row.items():
            if isinstance(value, str) and "cite" in value:
                value = _STRAY_TAG.sub("", CITE_TAG.sub(_replace, value))
            new_row[key] = value
        rows.append(new_row)
    return {**table, "rows": rows}, found
