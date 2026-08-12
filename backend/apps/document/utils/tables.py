"""Detección, extracción y linealización de tablas en PDFs.

Por qué existe
--------------
En una tabla el formato *es* el contenido: una grilla de zonificación significa
algo por la intersección entre la fila (``R1``) y la columna (``altura máxima``).
Aplanarla a texto posicional no la degrada, la destruye — quedan cifras sueltas
sin referencia, el corte de chunk separa los encabezados de sus datos, y una
pared de números tiene poca señal léxica para el embedding.

La solución no es preservar el dibujo de la tabla sino **linealizar cada fila
repitiendo los encabezados**, de modo que cada fila sea una oración
auto-contenida:

    Zona R1 — altura máxima: 12 m; FOS: 0,6; retiro de frente: 3 m

Así el embedding recupera semántica, partir la tabla deja de huerfanar datos y
la cita apunta a la fila exacta.

Costo
-----
Detección y extracción con PyMuPDF son **locales y gratuitas**: sirven para PDFs
digitales con tablas delimitadas, que son la mayoría. Las páginas escaneadas y
las tablas sin bordes quedan para un extractor pago (Textract ``AnalyzeDocument``
con ``TABLES``), que se decide por página y no está implementado todavía.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Sequence

logger = logging.getLogger(__name__)

TABLE_START = "<<<TABLE>>>"
TABLE_END = "<<<ENDTABLE>>>"

# Bloque de tabla completo, para poder tratarlo como una unidad aguas abajo.
TABLE_BLOCK_RE = re.compile(
    re.escape(TABLE_START) + r".*?" + re.escape(TABLE_END),
    re.DOTALL,
)

# Una tabla por debajo de esto es casi siempre un falso positivo del detector
# (un recuadro, un pie de figura, un borde decorativo).
MIN_ROWS = 2
MIN_COLS = 2

# El detector de PyMuPDF es generoso: sobre documentos reales marca como tabla
# regiones de prosa, listas de una sola columna y glosarios cuyo "encabezado" es
# en realidad la primera fila de datos. Promover eso a `encabezado: valor`
# fabrica afirmaciones falsas — mucho peor que aplanar. Estos umbrales rigen
# cuándo se confía en un encabezado y cuándo la región se deja como prosa.
MAX_HEADER_LEN = 60          # un encabezado real es una etiqueta, no una oración
MIN_NAMED_COLS_RATIO = 0.6   # mayoría de columnas con nombre, o no hay encabezado
MAX_AVG_CELL_LEN = 150       # celdas largas ⇒ es un bloque de texto maquetado
MIN_FILLED_COLS = 2          # una sola columna con datos no es una tabla

# Cuánto de un bloque de texto debe caer dentro de una tabla para considerarlo
# parte de ella y no volver a emitirlo como prosa.
OVERLAP_THRESHOLD = 0.5


def _clean_cell(value: Any) -> str:
    """Normaliza una celda: PyMuPDF devuelve None para celdas vacías."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _dedupe_headers(headers: Sequence[str]) -> List[str]:
    """Desambigua encabezados repetidos o vacíos para que la fila sea legible."""
    out: List[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(headers):
        name = _clean_cell(raw)
        if not name:
            name = f"col{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        out.append(name)
    return out


def headers_are_trustworthy(
    names: Sequence[Any],
    column_count: int,
    data_rows: Sequence[Sequence[Any]] = (),
) -> bool:
    """¿Se puede afirmar que estos nombres son encabezados de columna?

    Ante la duda, no. Un encabezado inventado convierte cada fila en una
    afirmación falsa; sin encabezado la fila queda como lista de valores, que es
    incompleto pero cierto.
    """
    clean = [_clean_cell(n) for n in names]
    named = [n for n in clean if n]

    if len(named) < 2:
        return False
    if len(named) / max(1, column_count) < MIN_NAMED_COLS_RATIO:
        return False
    # Una oración no es una etiqueta de columna.
    if any(len(n) > MAX_HEADER_LEN or n.endswith(".") for n in named):
        return False
    # Nombres repetidos delatan que se promovió una fila de datos.
    if len({n.lower() for n in named}) < len(named):
        return False

    # Si un "encabezado" también aparece como dato en el cuerpo, no es un
    # encabezado: es una fila que el detector ascendió. Así se cuela, por
    # ejemplo, el significado de una sigla en un glosario de dos columnas.
    values = {
        _clean_cell(cell).lower()
        for row in data_rows
        for cell in row
        if _clean_cell(cell)
    }
    if any(n.lower() in values for n in named):
        return False

    return True


def looks_like_data_table(rows: Sequence[Sequence[Any]]) -> bool:
    """Descarta regiones de prosa y listas de una sola columna."""
    if not rows:
        return False

    column_count = max((len(r) for r in rows), default=0)
    filled_columns = sum(
        1
        for i in range(column_count)
        if any(_clean_cell(r[i]) for r in rows if i < len(r))
    )
    if filled_columns < MIN_FILLED_COLS:
        return False

    cells = [_clean_cell(c) for row in rows for c in row]
    non_empty = [c for c in cells if c]
    if not non_empty:
        return False

    avg_len = sum(len(c) for c in non_empty) / len(non_empty)
    return avg_len <= MAX_AVG_CELL_LEN


def linearize_rows(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Convierte filas en líneas auto-contenidas, una por fila.

    La primera columna hace de etiqueta de fila (la zona, el indicador, el año)
    y el resto se emite como ``encabezado: valor``. Sin encabezados utilizables
    cae a filas separadas por ``|``, que es lo que ya hace el lector de DOCX.
    """
    clean_headers = _dedupe_headers(headers) if headers else []
    lines: List[str] = []

    for row in rows:
        cells = [_clean_cell(c) for c in row]
        if not any(cells):
            continue

        if not clean_headers:
            lines.append(" | ".join(c for c in cells if c))
            continue

        label = cells[0] if cells and cells[0] else ""
        pairs: List[str] = []
        for i, cell in enumerate(cells):
            if i == 0 or not cell:
                continue
            header = clean_headers[i] if i < len(clean_headers) else f"col{i + 1}"
            pairs.append(f"{header}: {cell}")

        if label and pairs:
            lines.append(f"{label} — " + "; ".join(pairs))
        elif pairs:
            lines.append("; ".join(pairs))
        elif label:
            lines.append(label)

    return "\n".join(lines)


def _bbox_overlap_ratio(block_bbox: Sequence[float], table_bbox: Sequence[float]) -> float:
    """Fracción del área del bloque que cae dentro de la tabla."""
    bx0, by0, bx1, by1 = block_bbox
    tx0, ty0, tx1, ty1 = table_bbox

    inter_w = min(bx1, tx1) - max(bx0, tx0)
    inter_h = min(by1, ty1) - max(by0, ty0)
    if inter_w <= 0 or inter_h <= 0:
        return 0.0

    block_area = (bx1 - bx0) * (by1 - by0)
    if block_area <= 0:
        return 0.0
    return (inter_w * inter_h) / block_area


def block_is_inside_tables(block_bbox: Sequence[float], table_bboxes: Sequence[Sequence[float]]) -> bool:
    """True si el bloque ya está representado por alguna tabla extraída."""
    return any(
        _bbox_overlap_ratio(block_bbox, tb) >= OVERLAP_THRESHOLD
        for tb in table_bboxes
    )


def extract_page_tables(page) -> tuple[List[str], List[Sequence[float]]]:
    """Extrae y lineariza las tablas de una página de PyMuPDF.

    Devuelve ``(bloques_de_texto, bboxes)``. Cada bloque viene envuelto en
    ``<<<TABLE>>>`` / ``<<<ENDTABLE>>>`` para que el chunker lo trate como una
    unidad. Los bboxes le sirven al parser para no volver a emitir ese mismo
    contenido como prosa.

    Nunca propaga excepciones: si la detección falla, la página se procesa como
    texto corrido, que es el comportamiento previo.
    """
    blocks: List[str] = []
    bboxes: List[Sequence[float]] = []

    try:
        found = page.find_tables()
    except Exception as exc:
        logger.debug("Detección de tablas falló en la página %s: %s", page.number, exc)
        return blocks, bboxes

    for table in getattr(found, "tables", []) or []:
        try:
            rows = table.extract() or []
            column_count = max((len(r) for r in rows), default=0)
            if len(rows) < MIN_ROWS or column_count < MIN_COLS:
                continue

            # Si no parece una tabla de datos, se deja intacta: el bloque sigue
            # el camino de prosa y no se registra su bbox, así que nada se pierde.
            if not looks_like_data_table(rows):
                continue

            header_obj = getattr(table, "header", None)
            headers: List[str] = []
            data_rows = list(rows)
            raw_names = getattr(header_obj, "names", None) if header_obj is not None else None

            body_rows = (
                rows[1:]
                if (raw_names and not getattr(header_obj, "external", False))
                else rows
            )
            if raw_names and headers_are_trustworthy(raw_names, column_count, body_rows):
                headers = list(raw_names)
                # Cuando el encabezado es parte del cuerpo, extract() lo devuelve
                # como primera fila y hay que sacarlo para no duplicarlo.
                data_rows = body_rows

            text = linearize_rows(headers, data_rows)
            if not text.strip():
                continue

            blocks.append(f"{TABLE_START}\n{text}\n{TABLE_END}")
            bboxes.append(table.bbox)
        except Exception as exc:
            logger.debug("Extracción de una tabla falló: %s", exc)
            continue

    return blocks, bboxes


def split_table_rows(table_body: str) -> List[str]:
    """Filas de un bloque de tabla ya linealizado."""
    return [line for line in (table_body or "").split("\n") if line.strip()]


def unwrap_table_block(paragraph: str) -> str | None:
    """Devuelve el cuerpo de un bloque de tabla, o None si no lo es."""
    text = (paragraph or "").strip()
    if text.startswith(TABLE_START) and text.endswith(TABLE_END):
        return text[len(TABLE_START): -len(TABLE_END)].strip()
    return None
