"""
Propone etiquetas de evidencia a los documentos que no tienen ninguna.

La etiqueta la elige quien carga el documento. Este comando es para lo que se
cargó antes, o sin elegirla: si los temas del documento sugieren una etiqueta
(``apps.document.services.default_evidence_tags_for``), se la asigna. Como las
operaciones heredan las etiquetas de sus documentos, etiquetar acá alcanza para
que los pasos que piden esa evidencia la encuentren en todas.

Es idempotente y nunca toca un documento que ya tenga alguna etiqueta: esa ya
es una decisión de alguien.

Uso
---
# Dry-run (cuenta qué se etiquetaría, no escribe nada):
python manage.py backfill_evidence_tags --dry-run

# Real:
python manage.py backfill_evidence_tags

# Acotado a los documentos de una operación:
python manage.py backfill_evidence_tags --project-slug mi-operacion
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.document.models import Document
from apps.document.services import default_evidence_tags_for
from apps.project.models import Project


class Command(BaseCommand):
    help = "Propone etiquetas de evidencia a los documentos que no tienen ninguna."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Muestra qué se etiquetaría sin escribir en la base.",
        )
        parser.add_argument(
            "--project-slug",
            default=None,
            help="Acotar a los documentos de una sola operación.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        project_slug: str | None = options["project_slug"]

        qs = Document.objects.filter(evidence_tags__isnull=True).only("id", "slug", "topics")
        if project_slug:
            project = Project.objects.filter(slug=project_slug).first()
            if project is None:
                self.stderr.write(f"No existe una operación con slug '{project_slug}'.")
                return
            qs = qs.filter(project_documents__project=project)

        total = 0
        tagged = 0
        by_tag: dict[str, int] = {}

        for doc in qs.distinct().iterator(chunk_size=500):
            total += 1
            proposed = default_evidence_tags_for(doc)
            if not proposed:
                continue
            tagged += 1
            for tag in proposed:
                by_tag[tag.slug] = by_tag.get(tag.slug, 0) + 1
            if not dry_run:
                doc.evidence_tags.set(proposed)
                self.stdout.write(f"  {doc.slug} → {', '.join(t.slug for t in proposed)}")

        verb = "se etiquetarían" if dry_run else "se etiquetaron"
        self.stdout.write("")
        self.stdout.write(
            f"{tagged} de {total} documentos sin etiqueta {verb} "
            f"({total - tagged} siguen sin ningún tema reconocible)."
        )
        if by_tag:
            self.stdout.write("Por etiqueta:")
            for slug, count in sorted(by_tag.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {slug}: {count}")
        if dry_run and tagged:
            self.stdout.write("\nDry-run: no se escribió nada. Repetí sin --dry-run para aplicar.")
