"""
Vuelve a proponer etiquetas de evidencia para los vínculos que quedaron sin ninguna.

La propuesta corre una sola vez, al vincular (ver
``apps.project.services.evidence_tags.apply_default_tags``): si en ese momento
el topic del documento no matcheaba ningún ``EvidenceTag.source_topics``,
el vínculo se quedó sin etiqueta para siempre — nadie vuelve a evaluarlo
después. Eso incluye la migración de datos 0010, que corrió una sola vez con
la lógica de matching de ese momento.

Este comando es la manera de recuperar terreno sin re-vincular nada: cuando el
matching mejora (``apps.document.services._document_topic_keys``) o se agregan
``source_topics`` nuevos a una etiqueta, correrlo vuelve a intentar sobre los
vínculos que siguen sin ninguna. Es idempotente y no toca los que ya tienen
alguna asignada — nunca pisa una decisión manual.

Uso
---
# Dry-run (cuenta qué se etiquetaría, no escribe nada):
python manage.py backfill_evidence_tags --dry-run

# Real:
python manage.py backfill_evidence_tags

# Acotado a una operación puntual:
python manage.py backfill_evidence_tags --project-slug mi-operacion
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.document.services import default_evidence_tags_for
from apps.project.models import Project, ProjectDocument


class Command(BaseCommand):
    help = (
        "Vuelve a proponer etiquetas de evidencia para los documentos de "
        "operaciones que quedaron sin ninguna."
    )

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
            help="Acotar a una sola operación.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        project_slug: str | None = options["project_slug"]

        qs = (
            ProjectDocument.objects.filter(tags__isnull=True)
            .select_related("document", "project")
        )
        if project_slug:
            project = Project.objects.filter(slug=project_slug).first()
            if project is None:
                self.stderr.write(f"No existe una operación con slug '{project_slug}'.")
                return
            qs = qs.filter(project=project)

        total = 0
        tagged = 0
        by_tag: dict[str, int] = {}

        for link in qs.iterator(chunk_size=500):
            total += 1
            proposed = default_evidence_tags_for(link.document)
            if not proposed:
                continue
            tagged += 1
            for tag in proposed:
                by_tag[tag.slug] = by_tag.get(tag.slug, 0) + 1
            if not dry_run:
                link.tags.set(proposed)
                self.stdout.write(
                    f"  {link.project.slug} / {link.document.slug} → "
                    f"{', '.join(t.slug for t in proposed)}"
                )

        verb = "se etiquetarían" if dry_run else "se etiquetaron"
        self.stdout.write("")
        self.stdout.write(
            f"{tagged} de {total} vínculos sin etiqueta {verb} "
            f"({total - tagged} siguen sin ningún topic reconocible)."
        )
        if by_tag:
            self.stdout.write("Por etiqueta:")
            for slug, count in sorted(by_tag.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {slug}: {count}")
        if dry_run and tagged:
            self.stdout.write("\nDry-run: no se escribió nada. Repetí sin --dry-run para aplicar.")
