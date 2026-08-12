"""Re-run the ingestion pipeline over selected documents.

Needed because ``process_document_chunks`` refuses to claim a document that is
already PROCESSING or DONE — the guard that stops duplicate dispatches also
blocks any deliberate reprocessing. This command resets the state first, so
documents stuck in PROCESSING (a worker died mid-run) can be recovered too.

Examples:
    # See what would run, without touching anything
    python manage.py reprocess_documents --ids 55,88,726 --dry-run

    # Everything that is not fully processed
    python manage.py reprocess_documents --status partial,error,processing

    # Run inline instead of dispatching to Celery (useful in a one-off ECS task)
    python manage.py reprocess_documents --ids 726 --sync
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.document.models import ChunkingStatus, Document
from apps.document.tasks import process_document_chunks


class Command(BaseCommand):
    help = "Reset and re-run document processing for the selected documents."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ids",
            default="",
            help="Comma-separated document ids.",
        )
        parser.add_argument(
            "--status",
            default="",
            help="Comma-separated chunking_status values to select "
                 "(pending, processing, done, partial, error).",
        )
        parser.add_argument(
            "--stale-processing-hours",
            type=int,
            default=0,
            help="With --status processing, only pick documents older than N hours.",
        )
        parser.add_argument(
            "--limit", type=int, default=0,
            help="Cap the number of documents (0 = no cap).",
        )
        parser.add_argument(
            "--sync", action="store_true",
            help="Run inline instead of dispatching to Celery.",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List the selection and exit without changing anything.",
        )

    def handle(self, *args, **options):
        ids = [int(v) for v in options["ids"].split(",") if v.strip()]
        statuses = [s.strip() for s in options["status"].split(",") if s.strip()]

        if not ids and not statuses:
            raise CommandError("Pass --ids and/or --status to select documents.")

        valid = set(ChunkingStatus.values)
        unknown = set(statuses) - valid
        if unknown:
            raise CommandError(f"Unknown status values: {', '.join(sorted(unknown))}")

        qs = Document.objects.all()
        if ids and statuses:
            from django.db.models import Q
            qs = qs.filter(Q(id__in=ids) | Q(chunking_status__in=statuses))
        elif ids:
            qs = qs.filter(id__in=ids)
        else:
            qs = qs.filter(chunking_status__in=statuses)

        hours = options["stale_processing_hours"]
        if ChunkingStatus.PROCESSING in statuses and not hours:
            # Resetting a document that a worker is actively chunking would put
            # two runs on the same rows. Only reach back for genuinely stale ones.
            hours = 2
            self.stdout.write(self.style.WARNING(
                "--status processing without --stale-processing-hours: "
                "defaulting to 2h so in-flight documents are left alone."
            ))
        if hours:
            from datetime import timedelta

            from django.utils import timezone

            cutoff = timezone.now() - timedelta(hours=hours)
            qs = qs.exclude(chunking_status=ChunkingStatus.PROCESSING, created_at__gt=cutoff)

        qs = qs.order_by("id")
        if options["limit"]:
            qs = qs[: options["limit"]]

        documents = list(qs)
        if not documents:
            self.stdout.write("No documents matched the selection.")
            return

        self.stdout.write(f"Selected {len(documents)} document(s):")
        for doc in documents:
            self.stdout.write(
                f"  {doc.id:>5}  {doc.chunking_status:<11} {(doc.name or '')[:60]}"
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing was changed."))
            return

        for doc in documents:
            # Clear the claim so the task's atomic guard lets this run through.
            Document.objects.filter(pk=doc.pk).update(
                chunking_status=ChunkingStatus.PENDING,
                chunking_done=False,
                last_error="",
            )
            if options["sync"]:
                result = process_document_chunks(doc.pk)
                self.stdout.write(f"  {doc.id:>5}  -> {result}")
            else:
                process_document_chunks.delay(doc.pk)
                self.stdout.write(f"  {doc.id:>5}  -> queued")

        verb = "processed" if options["sync"] else "queued"
        self.stdout.write(self.style.SUCCESS(f"{len(documents)} document(s) {verb}."))
