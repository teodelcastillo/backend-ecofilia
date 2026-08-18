"""
De dónde salió una cita: del offset de caracteres al número de página.

La API devuelve, por cada afirmación citada, el texto literal y su ubicación.
Sobre un PDF esa ubicación es una página; sobre texto plano es un rango de
caracteres. Un rango de caracteres no le sirve a nadie: quien revisa el informe
en CAF necesita abrir el documento en la página 47, no contar 213.480 caracteres.

Lo tentador sería mandar los PDF y dejar que la API numere las páginas. No
entra: los seis documentos del expediente suman 825 páginas contra un tope de
600 por pedido, y cada página de PDF se cobra como texto *más* imagen — del
orden de 1,2M a 2,5M de tokens contra los 554k que el mismo corpus ocupa como
texto. El PDF compraría números de página al precio de no poder mandar el
expediente.

No hace falta. El parser ya deja marcadores ``<<<PAGE:N>>>`` en el texto
extraído, que es de donde el chunker saca el ``page_number`` de cada fragmento.
Este módulo los usa para lo mismo un nivel más arriba: se quitan del texto que
viaja al modelo —así no ensucian el ``cited_text``— y se guarda a qué offset
empezaba cada página. Con eso, una cita por caracteres se traduce a página
exacta al costo en tokens del texto plano.

Los documentos procesados con el método viejo no tienen marcadores. No se
inventa una página para ellos: la cita queda con offset y sin página, y el
registro de la corrida lo dice.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

PAGE_MARKER = re.compile(r"<<<PAGE:(\d+)>>>\n*")


@dataclass
class PageMap:
    """El texto tal como viaja al modelo, y dónde empezaba cada página."""

    text: str
    # Offsets de inicio de cada página en ``text``, ordenados, y su número.
    offsets: list[int] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)

    @property
    def has_pages(self) -> bool:
        return bool(self.offsets)

    def page_at(self, index: int) -> int | None:
        """Número de página que contiene el carácter ``index``.

        Sin marcadores devuelve ``None`` —no una página inventada—: una
        referencia equivocada es peor que una referencia ausente, porque el
        lector la verifica, no la encuentra y deja de confiar en las demás.
        """
        if not self.offsets or index < 0:
            return None
        position = bisect.bisect_right(self.offsets, index) - 1
        if position < 0:
            return None
        return self.pages[position]

    def page_range(self, start: int, end: int) -> tuple[int | None, int | None]:
        """Páginas de inicio y fin de un rango de caracteres."""
        first = self.page_at(start)
        # ``end`` es exclusivo; el último carácter citado es ``end - 1``.
        last = self.page_at(max(start, end - 1))
        return first, last


def build_page_map(raw_text: str) -> PageMap:
    """Separa el texto de sus marcadores de página.

    Los marcadores se quitan a propósito. Si viajaran al modelo aparecerían
    dentro del ``cited_text`` que devuelve la API —la cita literal incluiría
    ``<<<PAGE:47>>>``— y además desplazarían los offsets respecto del documento
    que el lector abre. Se quitan del texto y se conserva la posición.
    """
    if not raw_text:
        return PageMap(text="")

    pieces: list[str] = []
    offsets: list[int] = []
    pages: list[int] = []
    cursor = 0  # posición en el texto limpio que se va armando
    last_end = 0

    for match in PAGE_MARKER.finditer(raw_text):
        chunk = raw_text[last_end : match.start()]
        pieces.append(chunk)
        cursor += len(chunk)
        offsets.append(cursor)
        pages.append(int(match.group(1)))
        last_end = match.end()

    pieces.append(raw_text[last_end:])
    return PageMap(text="".join(pieces), offsets=offsets, pages=pages)


def format_reference(page_start: int | None, page_end: int | None) -> str:
    """Cómo se nombra la ubicación de una cita en la interfaz."""
    if page_start is None:
        return "sin página"
    if page_end is None or page_end == page_start:
        return f"p. {page_start}"
    return f"pp. {page_start}–{page_end}"
