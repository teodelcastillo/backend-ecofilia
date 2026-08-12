"""
Document chunker — semantic boundary-aware, with metadata extraction.

Strategy
--------
1. Split on natural paragraph / section boundaries (double newlines, headings).
2. Track the current section heading so every chunk knows its ``title``.
3. Merge adjacent short paragraphs (below ``min_tokens``) to avoid tiny chunks.
4. Slide over single paragraphs that exceed ``max_tokens`` using a token window.
5. Extract cheap per-chunk keywords (stopword filter, ≥4 chars) and store them
   in ``SmartChunk.keywords`` for future use in lexical / hybrid retrieval.
6. Prepend the LLM-generated context summary to the embedding input (contextual
   retrieval) without altering the stored chunk content.
"""

from __future__ import annotations

import logging
import re
from typing import List

from apps.document.models import SmartChunk
from apps.document.utils.client_openia import embed_text, generate_chunk_context
from apps.document.utils.client_tiktoken import decode_text, encode_text, token_count
from apps.document.utils.tables import split_table_rows, unwrap_table_block

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_TOKENS: int = 500
MIN_TOKENS: int = 40       # below this, try to merge with the next paragraph
OVERLAP_TOKENS: int = 80   # token overlap between consecutive oversized paragraphs


# ---------------------------------------------------------------------------
# Heading detection
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(
    r"^"
    r"(?:#{1,6}\s+.+$"                          # Markdown: # Title
    r"|(?:\d+\.){1,3}\s+[A-ZÁÉÍÓÚ].+$"          # Numbered: 1.2.3 Title
    r"|[A-ZÁÉÍÓÚÑ\s\-]{4,80}$"                  # ALL CAPS line (4-80 chars)
    r")",
    re.MULTILINE | re.UNICODE,
)


def _is_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 120:
        return False
    # Numbered or Markdown headings take priority
    if re.match(r"^#{1,6}\s", stripped):
        return True
    # Numbered sections: "1. Title", "1.2 Title", "1.2. Title", "1.2) Title"
    if re.match(r"^\d+(?:\.\d+)*[\.\)]?\s+[A-ZÁÉÍÓÚA-Z]", stripped):
        return True
    # ALL CAPS: at least 4 chars, no lowercase, no sentence-ending punctuation
    if (
        stripped == stripped.upper()
        and len(stripped) >= 4
        and not stripped.endswith((".", ",", ";", ":", "?", "!"))
        and len(stripped.split()) <= 10
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Keyword extraction (no external deps)
# ---------------------------------------------------------------------------

_STOP_ES_EN = frozenset(
    {
        # ES
        "para", "como", "cual", "cuál", "cuáles", "cuales", "donde", "dónde",
        "cuando", "cuándo", "porque", "según", "segun", "entre", "sobre", "todo",
        "todos", "todas", "toda", "este", "esta", "esto", "ese", "esa", "eso",
        "tener", "tiene", "hacer", "decir", "puedes", "puede", "pueden", "favor",
        "informacion", "información", "lista", "necesito", "quisiera", "pero",
        "también", "tambien", "sino", "desde", "hacia", "hasta", "tras", "ante",
        "bajo", "cabe", "salvo", "mediante", "durante", "mediante", "aunque",
        # EN
        "what", "which", "where", "when", "with", "from", "into", "about",
        "please", "could", "would", "should", "have", "their", "there", "these",
        "those", "list", "show", "that", "this", "more", "also", "will", "been",
    }
)


def _extract_keywords(text: str, max_terms: int = 10) -> List[str]:
    """Cheap keyword extraction: unique words ≥4 chars, no stopwords."""
    tokens = re.findall(r"[a-záéíóúñü0-9]+", (text or "").lower())
    seen: set[str] = set()
    out: List[str] = []
    for t in tokens:
        if len(t) < 4 or t in _STOP_ES_EN or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


# ---------------------------------------------------------------------------
# Semantic paragraph splitter
# ---------------------------------------------------------------------------

_PAGE_MARKER_RE = re.compile(r"^<<<PAGE:(\d+)>>>$")


def _semantic_paragraphs(text: str) -> List[dict]:
    """
    Walk ``text`` paragraph by paragraph, tracking section headings and page numbers.

    Returns a list of ``{"text": ..., "title": ..., "tokens": int, "page": int|None}``.
    Merges adjacent short paragraphs (< MIN_TOKENS) into the same chunk.
    Splits long paragraphs (> MAX_TOKENS) with a sentence-aware fallback.

    ``<<<PAGE:N>>>`` markers emitted by the parser are consumed here and used to
    tag each segment with its source page number. They are never included in
    stored chunk content.
    """
    raw_paras = re.split(r"\n{2,}", (text or "").strip())
    raw_paras = [p.strip() for p in raw_paras if p.strip()]

    segments: List[dict] = []
    current_title = ""
    current_parts: List[str] = []
    current_tokens: int = 0
    current_page: int | None = None

    def _flush(parts: List[str], title: str) -> None:
        if not parts:
            return
        merged = "\n\n".join(parts)
        toks = token_count(merged)
        if toks >= MIN_TOKENS:
            segments.append({"text": merged, "title": title, "tokens": toks, "page": current_page, "kind": "prose"})
            return

        # Below MIN_TOKENS. This used to be dropped outright, which quietly
        # deleted content from documents built out of short articles separated
        # by headings — exactly how urban/legal codes are written. Fold the
        # fragment into the previous segment instead; only when that would
        # overflow the window does it become a small chunk of its own.
        # Nunca se funde con un segmento de tabla: mezclar prosa dentro de una
        # tabla linealizada rompe la propiedad de que cada línea es una fila.
        if (
            segments
            and segments[-1]["kind"] == "prose"
            and segments[-1]["tokens"] + toks <= MAX_TOKENS
        ):
            prev = segments[-1]
            prev["text"] = f"{prev['text']}\n\n{merged}"
            prev["tokens"] = token_count(prev["text"])
        else:
            segments.append({"text": merged, "title": title, "tokens": toks, "page": current_page, "kind": "prose"})

    def _emit_table(body: str, title: str) -> None:
        """Emite una tabla linealizada como segmentos propios.

        Cada línea ya es una fila auto-contenida (lleva sus encabezados), así que
        una tabla que excede la ventana se parte **por filas** y ningún trozo
        queda sin referencia. Nunca se corta una fila por la mitad.
        """
        rows = split_table_rows(body)
        if not rows:
            return

        group: List[str] = []
        group_tokens = 0
        for row in rows:
            row_tokens = token_count(row)
            if group and group_tokens + row_tokens > MAX_TOKENS:
                text = "\n".join(group)
                segments.append({
                    "text": text, "title": title,
                    "tokens": token_count(text), "page": current_page,
                    "kind": "table",
                })
                group, group_tokens = [], 0
            group.append(row)
            group_tokens += row_tokens

        if group:
            text = "\n".join(group)
            segments.append({
                "text": text, "title": title,
                "tokens": token_count(text), "page": current_page,
                "kind": "table",
            })

    def _slide_long(para: str, title: str) -> None:
        """
        Sentence-aware fallback for paragraphs exceeding MAX_TOKENS.

        Splits on natural sentence boundaries first (.  !  ?  …).  Each
        accumulated sentence group is flushed before exceeding MAX_TOKENS.
        If an individual sentence is itself longer than MAX_TOKENS (rare in ESG
        text), it falls back to a token window for that sentence only.
        When no sentence boundaries exist at all, the whole paragraph gets the
        pure token-sliding treatment.
        """
        _SENTENCE_RE = re.compile(
            r'(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÚÑ"\'\(])',
            re.UNICODE,
        )
        sentences = _SENTENCE_RE.split(para)

        if len(sentences) <= 1:
            # No sentence boundaries — token-window fallback (same as before)
            tokens = encode_text(para)
            i = 0
            while i < len(tokens):
                window = tokens[i: i + MAX_TOKENS]
                segments.append({"text": decode_text(window), "title": title, "tokens": len(window), "page": current_page, "kind": "prose"})
                i += MAX_TOKENS - OVERLAP_TOKENS
            return

        current_sentences: List[str] = []
        current_toks = 0

        for sentence in sentences:
            s_toks = token_count(sentence)

            if s_toks > MAX_TOKENS:
                # Flush accumulated sentences first
                if current_sentences:
                    merged = " ".join(current_sentences)
                    segments.append({"text": merged, "title": title, "tokens": token_count(merged), "page": current_page, "kind": "prose"})
                    current_sentences, current_toks = [], 0
                # Single sentence too long → token-window for this sentence only
                toks = encode_text(sentence)
                i = 0
                while i < len(toks):
                    window = toks[i: i + MAX_TOKENS]
                    segments.append({"text": decode_text(window), "title": title, "tokens": len(window), "page": current_page, "kind": "prose"})
                    i += MAX_TOKENS - OVERLAP_TOKENS
                continue

            if current_toks + s_toks > MAX_TOKENS and current_sentences:
                merged = " ".join(current_sentences)
                segments.append({"text": merged, "title": title, "tokens": token_count(merged), "page": current_page, "kind": "prose"})
                current_sentences, current_toks = [], 0

            current_sentences.append(sentence)
            current_toks += s_toks

        if current_sentences:
            merged = " ".join(current_sentences)
            segments.append({"text": merged, "title": title, "tokens": token_count(merged), "page": current_page, "kind": "prose"})

    for para in raw_paras:
        # ── Page marker (<<<PAGE:N>>>) — update current page, never store ──────
        page_match = _PAGE_MARKER_RE.match(para)
        if page_match:
            current_page = int(page_match.group(1))
            continue

        # ── Bloque de tabla ya linealizado por el parser ─────────────────────
        table_body = unwrap_table_block(para)
        if table_body is not None:
            _flush(current_parts, current_title)
            current_parts = []
            current_tokens = 0
            _emit_table(table_body, current_title)
            continue

        if _is_heading(para):
            # Flush current accumulation before starting a new section.
            # The heading also stays in the body of the next segment: it was
            # previously kept only as metadata, so ALL-CAPS article headers
            # ("ARTÍCULO 12", "ZONA R1") vanished from the retrievable text.
            _flush(current_parts, current_title)
            current_title = para
            current_parts = [para]
            current_tokens = token_count(para)
            continue

        para_tokens = token_count(para)

        if para_tokens > MAX_TOKENS:
            # Flush pending content first, then slide over this long paragraph
            _flush(current_parts, current_title)
            current_parts = []
            current_tokens = 0
            _slide_long(para, current_title)
            continue

        if current_tokens + para_tokens > MAX_TOKENS and current_parts:
            # Flush and start fresh, carrying the last part as overlap if small
            _flush(current_parts, current_title)
            last = current_parts[-1]
            last_toks = token_count(last)
            if last_toks <= OVERLAP_TOKENS:
                current_parts = [last]
                current_tokens = last_toks
            else:
                current_parts = []
                current_tokens = 0

        current_parts.append(para)
        current_tokens += para_tokens

    _flush(current_parts, current_title)
    return segments


# ---------------------------------------------------------------------------
# Legacy pure-token chunker (kept for backward-compat / tests)
# ---------------------------------------------------------------------------


def chunk_text(text: str, max_tokens: int = MAX_TOKENS, overlap: int = OVERLAP_TOKENS) -> List[str]:
    """
    Pure token-sliding chunker (legacy). New code should use
    ``_semantic_paragraphs`` via ``chunk_text_and_embed``.
    """
    tokens = encode_text(text)
    chunks: List[str] = []
    i = 0
    while i < len(tokens):
        chunk_tokens = tokens[i : i + max_tokens]
        chunks.append(decode_text(chunk_tokens))
        i += max_tokens - overlap
    return chunks


# ---------------------------------------------------------------------------
# Main entry point used by the processing pipeline
# ---------------------------------------------------------------------------


def chunk_text_and_embed(
    text: str,
    document_id: int,
    *,
    document_name: str = "",
    content_summary: str | None = None,
) -> List[SmartChunk]:
    """
    Segment, enrich and embed a document's text.

    Pipeline per chunk:
    1. Semantic paragraph split → preserves title / section context.
    2. LLM-generated context sentence (2-3 lines) → prepended to embedding
       input only; stored in ``context_summary`` for prompt construction.
    3. Cheap keyword extraction → stored in ``SmartChunk.keywords``.
    4. embed(context_summary + content) → stored in ``SmartChunk.embedding``.

    A special summary chunk (chunk_index=0) is prepended when a document
    content summary exists, so the whole-document intent is retrievable.
    """
    result: List[SmartChunk] = []
    idx = 0
    title = (document_name or "").strip()
    summary = (content_summary or "").strip()

    # --- Summary anchor chunk (chunk #0) ---
    if summary:
        parts: List[str] = []
        if title:
            parts.append(f"Documento: {title}")
        parts.append(f"Resumen general: {summary}")
        brief = "\n".join(parts)
        result.append(
            SmartChunk(
                document_id=document_id,
                chunk_index=idx,
                content=brief,
                title=title or None,
                keywords=_extract_keywords(brief),
                token_count=token_count(brief),
                embedding=embed_text(brief),
            )
        )
        idx += 1

    # --- Semantic segments ---
    segments = _semantic_paragraphs(text)

    for seg in segments:
        chunk_content: str = seg["text"]
        chunk_title: str = seg["title"]

        # LLM context summary (2-3 sentences situating the chunk in its doc)
        ctx = ""
        try:
            ctx = generate_chunk_context(
                chunk_content=chunk_content,
                doc_name=document_name,
                doc_summary=content_summary or "",
                chunk_index=idx,
                section_title=chunk_title or "",
            )
        except Exception as exc:
            logger.warning(
                "Chunk context generation failed for chunk %d: %s", idx, exc
            )

        embed_input = f"{ctx}\n\n{chunk_content}" if ctx else chunk_content
        keywords = _extract_keywords(chunk_content)

        result.append(
            SmartChunk(
                document_id=document_id,
                chunk_index=idx,
                chunk_type=seg.get("kind") or "prose",
                content=chunk_content,
                context_summary=ctx,
                title=chunk_title or None,
                keywords=keywords,
                token_count=token_count(chunk_content),
                embedding=embed_text(embed_input),
                page_number=seg.get("page"),
            )
        )
        idx += 1

    return result


def chunk_text_and_embed_origin(text: str, document_id: int) -> List[SmartChunk]:
    """Legacy entry point kept for backward compatibility."""
    raw_chunks = chunk_text(text)
    return [
        SmartChunk(
            document_id=document_id,
            chunk_index=i,
            content=chunk,
            token_count=len(chunk.split()),
            embedding=embed_text(chunk),
        )
        for i, chunk in enumerate(raw_chunks)
    ]
