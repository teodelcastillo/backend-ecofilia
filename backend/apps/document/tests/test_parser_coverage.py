"""Regression tests for the ingestion parser + chunker.

These cover the failure mode that shipped to production undetected: a PDF whose
pages are mostly unreadable still reported success, and the chunker silently
dropped short paragraphs.
"""
from __future__ import annotations

import os
import tempfile

from django.test import SimpleTestCase

from apps.document.utils.chunker import _semantic_paragraphs
from apps.document.utils.parser import ParseResult, parse_file_detailed


class ParseResultCoverageTests(SimpleTestCase):
    def test_coverage_ratios(self):
        result = ParseResult(text="x" * 1000, page_count=100, pages_with_text=8)
        self.assertAlmostEqual(result.page_coverage, 0.08)
        self.assertAlmostEqual(result.chars_per_page, 10.0)

    def test_non_paginated_formats_report_full_coverage(self):
        """DOCX/TXT carry no page stats and must not trip the coverage gate."""
        result = ParseResult(text="hello", page_count=0)
        self.assertEqual(result.page_coverage, 1.0)

    def test_txt_roundtrip_reports_parser(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write("Primera línea.\n\nSegunda línea.")
            path = fh.name
        try:
            result = parse_file_detailed(path)
            self.assertEqual(result.parser, "txt")
            self.assertIn("Primera línea", result.text)
            self.assertEqual(result.page_count, 0)
        finally:
            os.unlink(path)


class ChunkerContentPreservationTests(SimpleTestCase):
    def test_short_paragraphs_are_not_dropped(self):
        """Short articles between headings must survive, merged not deleted."""
        text = "\n\n".join([
            "ARTICULO PRIMERO",
            "La altura máxima en zona R1 es de 12 metros.",
            "ARTICULO SEGUNDO",
            "El retiro de frente será de 3 metros.",
        ])
        segments = _semantic_paragraphs(text)
        joined = " ".join(s["text"] for s in segments)
        self.assertIn("12 metros", joined)
        self.assertIn("3 metros", joined)

    def test_heading_text_is_retrievable(self):
        """ALL-CAPS headings are metadata *and* content."""
        text = "ZONA RESIDENCIAL R1\n\n" + ("El uso permitido es residencial. " * 30)
        segments = _semantic_paragraphs(text)
        joined = " ".join(s["text"] for s in segments)
        self.assertIn("ZONA RESIDENCIAL R1", joined)

    def test_page_markers_tag_segments_and_are_stripped(self):
        body = "Contenido de la página. " * 30
        text = f"<<<PAGE:7>>>\n\n{body}"
        segments = _semantic_paragraphs(text)
        self.assertTrue(segments)
        self.assertEqual(segments[0]["page"], 7)
        self.assertNotIn("<<<PAGE", segments[0]["text"])

    def test_no_content_lost_across_long_document(self):
        """Every sentence marker must appear somewhere in the output."""
        paras = [f"Parrafo numero {i} con contenido suficiente. " * 5 for i in range(20)]
        text = "\n\n".join(paras)
        segments = _semantic_paragraphs(text)
        joined = " ".join(s["text"] for s in segments)
        for i in range(20):
            self.assertIn(f"Parrafo numero {i} ", joined)
