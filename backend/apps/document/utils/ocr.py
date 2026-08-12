"""OCR fallback for scanned PDFs, via Amazon Textract.

Only reached when the regular text extraction path comes up short: roughly 3%
of the library is scanned image-only PDFs (municipal plans, older NDC/NAP
submissions) where PyMuPDF has nothing to read.

Textract's asynchronous API is used because it is the only one that accepts
multi-page PDFs straight from S3 — the file is already there, so nothing is
re-uploaded. Results are assembled with ``<<<PAGE:N>>>`` markers, the same
contract the parser emits, so chunks keep their page numbers.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class OcrError(RuntimeError):
    """Raised when Textract cannot produce text for a document."""


@dataclass
class OcrResult:
    text: str = ""
    page_count: int = 0
    pages_with_text: int = 0


def ocr_enabled() -> bool:
    return str(os.environ.get("DOCUMENT_OCR_ENABLED", "1")).lower() in (
        "1", "true", "yes", "on",
    )


def max_ocr_pages() -> int:
    """Hard ceiling so a pathological upload cannot run up an OCR bill."""
    return int(os.environ.get("OCR_MAX_PAGES", "1000"))


def _region() -> str:
    return (
        os.environ.get("AWS_TEXTRACT_REGION")
        or os.environ.get("AWS_S3_REGION_NAME")
        or "us-east-2"
    )


def _client():
    import boto3

    return boto3.client("textract", region_name=_region())


def ocr_pdf_from_s3(
    bucket: str,
    key: str,
    *,
    poll_interval: float = 5.0,
    timeout: float = 1800.0,
) -> OcrResult:
    """Run Textract over an S3-hosted PDF and return its text with page markers.

    Raises ``OcrError`` on job failure, timeout, or page-limit overrun.
    """
    client = _client()

    response = client.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    job_id = response["JobId"]
    logger.info("Textract job %s started for s3://%s/%s", job_id, bucket, key)

    deadline = time.monotonic() + timeout
    while True:
        result = client.get_document_text_detection(JobId=job_id, MaxResults=1)
        status = result.get("JobStatus")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "PARTIAL_SUCCESS"):
            raise OcrError(
                f"Textract job {job_id} finished as {status}: "
                f"{result.get('StatusMessage') or 'sin detalle'}"
            )
        if time.monotonic() > deadline:
            raise OcrError(f"Textract job {job_id} exceeded the {timeout:.0f}s budget.")
        time.sleep(poll_interval)

    page_count = int(result.get("DocumentMetadata", {}).get("Pages", 0) or 0)
    if page_count > max_ocr_pages():
        raise OcrError(
            f"El documento tiene {page_count} páginas y supera el límite de OCR "
            f"({max_ocr_pages()})."
        )

    # Collect LINE blocks grouped by page. Textract paginates its own response,
    # and block order within a page already follows reading order.
    lines_by_page: dict[int, list[str]] = {}
    next_token = None
    while True:
        kwargs = {"JobId": job_id, "MaxResults": 1000}
        if next_token:
            kwargs["NextToken"] = next_token
        page_result = client.get_document_text_detection(**kwargs)

        for block in page_result.get("Blocks", []):
            if block.get("BlockType") != "LINE":
                continue
            text = (block.get("Text") or "").strip()
            if text:
                lines_by_page.setdefault(int(block.get("Page", 1)), []).append(text)

        next_token = page_result.get("NextToken")
        if not next_token:
            break

    parts: list[str] = []
    pages_with_text = 0
    for page_num in sorted(lines_by_page):
        page_text = "\n".join(lines_by_page[page_num])
        if len(page_text.strip()) < 40:
            continue
        pages_with_text += 1
        parts.append(f"<<<PAGE:{page_num}>>>\n\n{page_text}")

    text = "\n\n".join(parts).strip()
    logger.info(
        "Textract job %s: %d/%d pages with text, %d chars.",
        job_id, pages_with_text, page_count, len(text),
    )
    return OcrResult(
        text=text,
        page_count=page_count or len(lines_by_page),
        pages_with_text=pages_with_text,
    )
