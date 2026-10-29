"""
Completa con IA el tipo de operación de las operaciones que no lo tienen.

El tipo pasó a ser obligatorio: filtra y agrupa el listado y el tablero. Las
operaciones cargadas antes no lo tienen; este comando lo lee del documento
principal (``ai_fill``) y lo deja marcado como sugerido
(``tipo_operacion_sugerido``) para que el ejecutivo lo confirme o lo corrija.

Nunca pisa un tipo que alguien cargó. Las operaciones sin documento principal
se saltean.

Uso
---
# Dry-run (lista qué operaciones se completarían, sin llamar al modelo):
python manage.py backfill_tipo_operacion --dry-run

# Real:
python manage.py backfill_tipo_operacion

# Acotado a una operación:
python manage.py backfill_tipo_operacion --project-slug mi-operacion
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.project.models import Project
from apps.project.services.ai_fill import TIPO_SUGERIDO_KEY, ai_fill_project, save_extracted


class Command(BaseCommand):
    help = "Completa con IA el tipo de operación que falta, marcado como sugerido."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", default=False)
        parser.add_argument("--project-slug", default=None)

    def handle(self, *args, **options):
        qs = Project.objects.filter(blueprint_document__isnull=False).order_by("id")
        if options["project_slug"]:
            qs = qs.filter(slug=options["project_slug"])

        pendientes = [
            p
            for p in qs
            if not str((p.context_notes or {}).get("tipo_operacion") or "").strip()
        ]
        self.stdout.write(f"{len(pendientes)} operaciones sin tipo con documento principal.")
        if options["dry_run"]:
            for project in pendientes:
                self.stdout.write(f"  - {project.slug}")
            return

        completadas = 0
        for project in pendientes:
            try:
                results = ai_fill_project(project, ["tipo_operacion"])
            except (ValueError, RuntimeError) as exc:
                self.stdout.write(self.style.WARNING(f"  {project.slug}: {exc}"))
                continue
            if not results.get("tipo_operacion"):
                self.stdout.write(f"  {project.slug}: el documento no permite saberlo")
                continue
            results[TIPO_SUGERIDO_KEY] = "ia"
            if save_extracted(project.id, results, only_missing=True).get("tipo_operacion"):
                completadas += 1
                self.stdout.write(f"  {project.slug}: {results['tipo_operacion']}")
        self.stdout.write(self.style.SUCCESS(f"{completadas} operaciones completadas."))
