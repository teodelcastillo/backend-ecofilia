"""
Run a tiny offline RAG evaluation from a JSON file.

JSON schema (list of cases):

[
  {
    "question": "¿Qué dice el reporte 2024 sobre emisiones?",
    "expected_document_slugs": ["reporte-2024"],
    "expected_keywords": ["emisiones", "alcance"],
    "notes": "smoke test"
  }
]

Usage:
  python manage.py rag_eval --user-email owner@example.com --cases evals/cases.json
  python manage.py rag_eval --user-email owner@example.com --cases evals/cases.json --top-n 16

The command resolves the user's allowed documents (own + public + shared +
shared-via-projects) and runs the full pipeline against each case.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.chat.services.rag_evaluation import RagEvalCase, run_eval
from apps.document.models import Document

User = get_user_model()


class Command(BaseCommand):
    help = "Run an offline RAG evaluation against a JSON dataset."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-email",
            required=True,
            help="Email of the user whose document scope will be used.",
        )
        parser.add_argument(
            "--cases",
            required=True,
            help="Path to a JSON file with the eval cases.",
        )
        parser.add_argument(
            "--top-n",
            type=int,
            default=12,
            help="Top-N chunks to retrieve per case (default: 12).",
        )

    def handle(self, *args, **options):
        email = options["user_email"]
        cases_path = Path(options["cases"]).expanduser().resolve()
        top_n = options["top_n"]

        if not cases_path.exists():
            raise CommandError(f"Cases file not found: {cases_path}")

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(f"User not found: {email}") from exc

        try:
            raw_cases = json.loads(cases_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {cases_path}: {exc}") from exc

        cases = [
            RagEvalCase(
                question=str(item.get("question", "")),
                expected_document_slugs=list(item.get("expected_document_slugs", [])),
                expected_keywords=list(item.get("expected_keywords", [])),
                notes=str(item.get("notes", "")),
            )
            for item in raw_cases
            if isinstance(item, dict) and item.get("question")
        ]

        if not cases:
            raise CommandError("No valid cases found in the input file.")

        # Document scope mirrors what the chat/RAG endpoints already enforce.
        from apps.document.services import visible_documents_q

        if user.is_staff:
            docs_qs = Document.objects.all()
        else:
            docs_qs = Document.objects.filter(visible_documents_q(user)).distinct()

        report = run_eval(cases, user=user, allowed_documents=docs_qs, top_n=top_n)
        self.stdout.write(report.summary())
        self.stdout.write("")
        for r in report.results:
            self.stdout.write(
                f"- {r.case.question[:80]!r}  "
                f"cov={r.coverage:.2f}  kw={r.keyword_recall:.2f}  "
                f"sources={r.unique_sources}  chunks={r.num_chunks}  "
                f"lat={r.latency_seconds:.2f}s"
            )
