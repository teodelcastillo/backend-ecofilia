"""
Phase 0 — Quality & coverage evaluation runner.

Runs the *full* RAG path (retrieval + generation) against a gold dataset and
scores answer recall, provenance, and abstention/faithfulness. See
``apps.chat.services.rag_eval_quality`` for the metric definitions and
``backend/evals/README.md`` for the case format.

Usage:
  python manage.py rag_eval_quality --user-email owner@example.com \
      --cases evals/cases.json

  # Record a baseline, then later compare against it:
  python manage.py rag_eval_quality --user-email me@x.com --cases evals/cases.json \
      --out evals/baseline.json
  python manage.py rag_eval_quality --user-email me@x.com --cases evals/cases.json \
      --baseline evals/baseline.json

  # Offline retrieval-only (no LLM calls at all):
  python manage.py rag_eval_quality --user-email me@x.com --cases evals/cases.json \
      --skip-generation

The document scope mirrors exactly what the chat/RAG endpoints enforce
(own + public + shared + shared-via-projects).
"""
from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.chat.services.rag_eval_quality import (
    QualityCase,
    diff_against_baseline,
    run_quality_eval,
)
from apps.document.models import Document

User = get_user_model()


class Command(BaseCommand):
    help = "Run the Phase 0 quality + coverage RAG evaluation against a JSON dataset."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True,
                            help="User whose document scope will be used.")
        parser.add_argument("--cases", required=True,
                            help="Path to the JSON gold dataset.")
        parser.add_argument("--top-n", type=int, default=12,
                            help="Top-N chunks to retrieve per case (default: 12).")
        parser.add_argument("--out", default="",
                            help="Write the full JSON report to this path "
                                 "(use to record a baseline).")
        parser.add_argument("--baseline", default="",
                            help="Compare the run against this baseline JSON report.")
        parser.add_argument("--skip-generation", action="store_true",
                            help="Retrieval metrics only; no LLM calls.")
        parser.add_argument("--skip-judge", action="store_true",
                            help="Generate answers but skip the LLM-judge metrics.")

    def handle(self, *args, **options):
        cases_path = Path(options["cases"]).expanduser().resolve()
        if not cases_path.exists():
            raise CommandError(f"Cases file not found: {cases_path}")

        try:
            user = User.objects.get(email=options["user_email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"User not found: {options['user_email']}") from exc

        try:
            raw = json.loads(cases_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {cases_path}: {exc}") from exc
        if not isinstance(raw, list):
            raise CommandError("The cases file must be a JSON list of case objects.")

        cases = [
            QualityCase.from_dict(item)
            for item in raw
            if isinstance(item, dict) and item.get("question")
        ]
        if not cases:
            raise CommandError("No valid cases found (each needs a 'question').")

        docs_qs = self._document_scope(user)

        report = run_quality_eval(
            cases,
            user=user,
            allowed_documents=docs_qs,
            top_n=options["top_n"],
            skip_generation=options["skip_generation"],
            skip_judge=options["skip_judge"],
        )

        self.stdout.write(report.summary())
        self.stdout.write("")
        self.stdout.write("Por tipo de tarea:")
        for task_type, stats in report.by_task_type().items():
            self.stdout.write(
                f"  {task_type:18s} n={stats['n']:<3} "
                f"ret_recall={stats['retrieval_recall_docs']} "
                f"answer_recall={stats['answer_recall']} "
                f"cite_correct={stats['citation_correctness']}"
            )

        self.stdout.write("")
        for r in report.results:
            cid = r.case.id or r.case.question[:48]
            self.stdout.write(
                f"- {cid!r:52s} "
                f"ret={r.retrieval_recall_docs:.2f} "
                f"ans={'—' if r.answer_recall is None else f'{r.answer_recall:.2f}'} "
                f"cite={'—' if r.citation_correctness is None else f'{r.citation_correctness:.2f}'} "
                f"chunks={r.num_chunks}"
                + (f"  ERROR={r.error}" if r.error else "")
            )

        report_dict = report.to_dict()

        if options["out"]:
            out_path = Path(options["out"]).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(report_dict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.stdout.write(self.style.SUCCESS(f"\nReporte guardado en {out_path}"))

        if options["baseline"]:
            base_path = Path(options["baseline"]).expanduser().resolve()
            if not base_path.exists():
                raise CommandError(f"Baseline not found: {base_path}")
            baseline = json.loads(base_path.read_text(encoding="utf-8"))
            self.stdout.write("")
            for line in diff_against_baseline(report_dict, baseline):
                self.stdout.write(line)

    @staticmethod
    def _document_scope(user):
        """Mirror the scope enforced by the chat/RAG endpoints."""
        from apps.document.services import visible_documents_q

        if user.is_staff:
            return Document.objects.all()
        return Document.objects.filter(visible_documents_q(user)).distinct()
