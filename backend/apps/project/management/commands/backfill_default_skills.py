"""
Asigna los agentes por defecto de una organización a los proyectos que
nacieron sin ninguno.

Una operación arranca con `Organization.default_project_skills` a través de
`_default_skills_for(project.owner)` — ver `apps.project.api.serializers` —
que mira la organización de quien la crea. Eso cubre al ejecutivo CAF típico,
pero no a quien crea la operación sin tener una organización asignada —
típicamente alguien de staff armándola desde el mismo formulario que usaría
un ejecutivo. El resultado es una operación sin ningún agente y, en el portal
CAF, sin ninguna forma de asignarle uno después: la migración 0007
("seed_caf_default_project_skills") ya resolvió esto una vez, pero sólo para
las que existían en ese momento — cualquier operación huérfana creada después
sigue así hasta que alguien la note.

Este comando hace lo mismo que esa migración, pero re-ejecutable y para
cualquier organización con `default_project_skills` configurado, no sólo CAF.
Idempotente y conservador: sólo toca proyectos con `enabled_skills` vacío —
uno con al menos un agente ya asignado refleja una elección deliberada y se
deja como está.

Uso
---
# Dry-run (cuenta qué se asignaría, no escribe nada):
python manage.py backfill_default_skills --dry-run

# Real, todas las organizaciones con defaults configurados:
python manage.py backfill_default_skills

# Acotado a una organización:
python manage.py backfill_default_skills --org-slug caf
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.project.models import Project
from apps.user.models import Organization


class Command(BaseCommand):
    help = (
        "Asigna los agentes por defecto de su organización a los proyectos "
        "que quedaron sin ningún agente."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Muestra qué se asignaría sin escribir en la base.",
        )
        parser.add_argument(
            "--org-slug",
            default=None,
            help="Acotar a una sola organización.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        org_slug: str | None = options["org_slug"]

        orgs = Organization.objects.filter(default_project_skills__isnull=False)
        if org_slug:
            orgs = orgs.filter(slug=org_slug)
        orgs = orgs.distinct()

        if not orgs.exists():
            self.stdout.write("Ninguna organización con default_project_skills configurado.")
            return

        total_fixed = 0
        for org in orgs:
            defaults = list(org.default_project_skills.all())
            if not defaults:
                continue
            orphans = (
                Project.objects.filter(owner__organization=org, enabled_skills__isnull=True)
                .distinct()
            )
            count = orphans.count()
            if count == 0:
                self.stdout.write(f"{org.name}: sin operaciones huérfanas.")
                continue

            verb = "se les asignaría" if dry_run else "se les asignó"
            self.stdout.write(
                f"{org.name}: {count} operación(es) sin agente — {verb} "
                f"{', '.join(s.slug for s in defaults)}"
            )
            if not dry_run:
                for project in orphans:
                    project.enabled_skills.set(defaults)
                    self.stdout.write(f"  {project.slug}")
            total_fixed += count

        if dry_run and total_fixed:
            self.stdout.write("\nDry-run: no se escribió nada. Repetí sin --dry-run para aplicar.")
