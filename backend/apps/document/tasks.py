from __future__ import annotations
import logging
import os
import tempfile
import time
import openai
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.db import transaction
from apps.document.models import Document, SmartChunk, ChunkingStatus
from apps.document.utils.chunker import chunk_text_and_embed
from apps.document.utils.client_openia import (
    embed_text,
    generate_chunk_context,
    generate_document_content_summary,
)
from apps.document.utils.parser import parse_file
from apps.document.utils.region_detector import detect_country_region

logger = logging.getLogger(__name__)


def _document_auto_summary_enabled() -> bool:
    return str(os.environ.get("DOCUMENT_AUTO_SUMMARY", "1")).lower() in (
        "1",
        "true",
        "yes",
    )

@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def process_document_chunks(self, doc_id: int) -> str:
    tmp_path = None
    try:
        # Atomically claim the document on the first attempt only. Two
        # dispatches racing for the same doc_id (double-click, redelivery)
        # will both reach this line, but only one UPDATE can flip a
        # non-PROCESSING/DONE row to PROCESSING — the loser exits here
        # instead of racing the winner into the chunk_index unique
        # constraint below. On Celery-internal retries (self.retry) the
        # status is already PROCESSING from the first attempt, so we skip
        # the claim and just continue.
        if self.request.retries == 0:
            claimed = (
                Document.objects.filter(pk=doc_id)
                .exclude(chunking_status__in=[ChunkingStatus.PROCESSING, ChunkingStatus.DONE])
                .update(chunking_status=ChunkingStatus.PROCESSING)
            )
            if not claimed:
                logger.info(
                    "Document %s already processing or done; skipping duplicate run.",
                    doc_id,
                )
                return "already_claimed"

        doc = Document.objects.get(pk=doc_id)

        # Ensure there's a file
        if not doc.file:
            raise FileNotFoundError("Document has no attached file.")

        # --- Create a temp file with the SAME extension so parse_file() can detect type ---
        # Prefer extension from the stored file key; fallback to doc.name if needed
        ext = os.path.splitext(doc.file.name or "")[1]
        if not ext:
            ext = os.path.splitext(doc.name or "")[1]

        # Stream from storage (S3/local) to temp file with preserved suffix
        with doc.file.open("rb") as f_src, tempfile.NamedTemporaryFile(suffix=ext or "", delete=False) as f_tmp:
            tmp_path = f_tmp.name
            for chunk in iter(lambda: f_src.read(1024 * 1024), b""):  # 1MB chunks
                f_tmp.write(chunk)

        text = parse_file(tmp_path) or ""
        # Postgres text/varchar columns reject NUL (0x00) bytes outright;
        # some PDF extractors emit them for malformed/binary-embedded content.
        text = text.replace("\x00", "")

        # ── Early exit: no extractable text ──────────────────────────────────
        if not text.strip():
            logger.warning("Document %s: no extractable text after parsing.", doc_id)
            Document.objects.filter(pk=doc_id).update(
                last_error=(
                    "No se pudo extraer texto del archivo. "
                    "El archivo puede estar vacío, protegido o en un formato no soportado."
                ),
                chunking_status=ChunkingStatus.ERROR,
            )
            return "empty"

        # ── Auto-detect region (only when not already set by the user) ──────
        # Pure regex scan of the doc name + first characters of extracted text;
        # no LLM call, negligible CPU cost.
        if not (doc.region or "").strip():
            detected_region = detect_country_region(doc.name or "", text)
            if detected_region:
                Document.objects.filter(pk=doc_id).update(region=detected_region)
                logger.info(
                    "Document %s: auto-detected region=%r", doc_id, detected_region
                )

        content_summary = ""
        if _document_auto_summary_enabled() and text.strip():
            try:
                content_summary = generate_document_content_summary(
                    title=doc.name or doc.slug or "Sin título",
                    body_text=text,
                )
            except Exception as sum_exc:
                logger.warning(
                    "Document %s: resumen automático omitido (%s)",
                    doc_id,
                    sum_exc,
                )

        chunks = chunk_text_and_embed(
            text,
            doc.id,
            document_name=doc.name or "",
            content_summary=content_summary or None,
        ) or []
        if not chunks:
            logger.warning("No chunks produced for document %s.", doc_id)

        with transaction.atomic():
            # Delete any previously-created chunks before inserting the fresh batch.
            # Makes the task idempotent: safe if SQS redelivers the message or the
            # task is manually retried. The unique_together DB constraint is the
            # last-resort guard; this delete is the application-level guard.
            SmartChunk.objects.filter(document_id=doc_id).delete()

            if chunks:
                SmartChunk.objects.bulk_create(chunks, batch_size=1000)

            (Document.objects
                .filter(pk=doc_id)
                .update(
                    extracted_text=text,
                    content_summary=content_summary,
                    chunking_done=True,
                    chunking_status=ChunkingStatus.DONE,
                    last_error=""
                ))

        logger.info("Chunking completed for document %s (%d chunks).", doc_id, len(chunks))
        return "ok"

    except Document.DoesNotExist:
        logger.error("Document %s not found for chunking.", doc_id)
        return "missing"

    except (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
        openai.RateLimitError,
    ) as e:
        # Transient network/upstream errors: retry with backoff instead of
        # failing on the first hiccup. chunking_status stays PROCESSING so
        # a concurrent dispatch can't jump the claim while we retry.
        logger.warning(
            "Transient error chunking document %s (attempt %d/%d): %s",
            doc_id, self.request.retries + 1, self.max_retries + 1, e,
        )
        try:
            # No exc= here: Celery re-raises `exc` as-is once retries are
            # exhausted instead of MaxRetriesExceededError, which would
            # bypass this except clause and leave the document stuck in
            # PROCESSING with no last_error recorded.
            raise self.retry(countdown=30 * (2 ** self.request.retries))
        except MaxRetriesExceededError:
            logger.exception("Failed to chunk document %s after retries: %s", doc_id, e)
            try:
                Document.objects.filter(pk=doc_id).update(
                    last_error=str(e),
                    chunking_status=ChunkingStatus.ERROR
                )
            except Exception:
                pass
            return "error"

    except Exception as e:
        logger.exception("Failed to chunk document %s: %s", doc_id, e)
        try:
            Document.objects.filter(pk=doc_id).update(
                last_error=str(e),
                chunking_status=ChunkingStatus.ERROR
            )
        except Exception:
            pass
        return "error"

    finally:
        # Clean up temp file if created
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                logger.warning("Could not remove temp file %s", tmp_path)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def backfill_chunk_context_for_document(self, doc_id: int, batch_size: int = 50) -> str:
    try:
        doc = Document.objects.get(pk=doc_id)
    except Document.DoesNotExist:
        logger.error("Backfill: document %s not found.", doc_id)
        return "missing"

    doc_name = doc.name or doc.slug or ""
    doc_summary = doc.content_summary or ""

    qs = SmartChunk.objects.filter(
        document_id=doc_id,
        context_summary="",
        chunk_index__gt=0,   # skip the summary anchor (chunk #0) — it has no context to generate
    ).order_by("chunk_index")

    total = qs.count()
    if total == 0:
        logger.info("Backfill: document %s has no chunks pending.", doc_id)
        return "already_done"

    logger.info("Backfill: document %s — %d chunks pending.", doc_id, total)
    processed = 0

    while True:
        batch = list(qs[:batch_size])
        if not batch:
            break

        for chunk in batch:
            ctx = generate_chunk_context(
                chunk_content=chunk.content or "",
                doc_name=doc_name,
                doc_summary=doc_summary,
                chunk_index=chunk.chunk_index,
            )
            if not ctx:
                continue

            embed_input = f"{ctx}\n\n{chunk.content}"
            new_embedding = embed_text(embed_input)

            SmartChunk.objects.filter(pk=chunk.pk).update(
                context_summary=ctx,
                embedding=new_embedding,
            )
            processed += 1
            sleep_secs = float(os.environ.get("BACKFILL_CHUNK_SLEEP", "2.0"))
            time.sleep(sleep_secs)

    logger.info(
        "Backfill: document %s complete — %d/%d chunks enriched.",
        doc_id, processed, total,
    )
    return f"ok:{processed}/{total}"
