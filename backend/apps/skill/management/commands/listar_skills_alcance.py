"""
Lista cada asistente con su alcance, institución y dueño, para que el dueño del
producto confirme cuáles abrir al público.

Después de la fase 0 todo lo que no tenía dueño quedó en ``platform`` (lo ve
sólo el superadmin): no había ninguna marca de «público». Este comando es el
paso que falta.

    python manage.py listar_skills_alcance
    python manage.py listar_skills_alcance --hacer-publicas slug1,slug2 --dry-run
    python manage.py listar_skills_alcance --hacer-publicas slug1,slug2

La columna «habilitada para» muestra las instituciones restringidas que la
tienen asignada: un asistente de plataforma asignado a una institución no lo ve
nadie de esa institución hasta que se haga público o pase a ser de ella.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.skill.models import Skill, SkillScope


class Command(BaseCommand):
    help = "Lista los asistentes por alcance y, con --hacer-publicas, los abre al público."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hacer-publicas",
            default="",
            help="Slugs separados por coma que pasan a alcance público.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra qué cambiaría sin escribir nada.",
        )

    def handle(self, *args, **options):
        slugs = [s.strip() for s in (options["hacer_publicas"] or "").split(",") if s.strip()]
        if slugs:
            self._hacer_publicas(slugs, dry_run=options["dry_run"])
            return
        self._listar()

    def _listar(self):
        skills = (
            Skill.objects.select_related("organization", "owner")
            .prefetch_related("enabled_for_organizations")
            .order_by("scope", "organization__slug", "slug")
        )
        rows = [
            (
                skill.slug,
                skill.scope,
                skill.organization.slug if skill.organization_id else "-",
                skill.owner.email if skill.owner_id else "-",
                ",".join(sorted(o.slug for o in skill.enabled_for_organizations.all())) or "-",
            )
            for skill in skills
        ]
        header = ("slug", "alcance", "institución", "dueño", "habilitada para")
        widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
        line = "  ".join("{:<%d}" % w for w in widths)
        self.stdout.write(line.format(*header))
        for row in rows:
            self.stdout.write(line.format(*row))
        counts = {scope: 0 for scope in SkillScope.values}
        for row in rows:
            counts[row[1]] = counts.get(row[1], 0) + 1
        self.stdout.write("")
        self.stdout.write(
            "Total: " + ", ".join(f"{scope}={n}" for scope, n in counts.items())
        )

    def _hacer_publicas(self, slugs: list[str], *, dry_run: bool):
        found = {s.slug: s for s in Skill.objects.filter(slug__in=slugs).select_related("organization")}
        missing = [slug for slug in slugs if slug not in found]
        if missing:
            raise CommandError(f"No existen: {', '.join(missing)}. No se cambió nada.")
        prefix = "[dry-run] " if dry_run else ""
        with transaction.atomic():
            for slug in slugs:
                skill = found[slug]
                if skill.scope == SkillScope.PUBLIC:
                    self.stdout.write(f"{prefix}{slug}: ya es pública")
                    continue
                before = skill.scope + (f" ({skill.organization.slug})" if skill.organization_id else "")
                self.stdout.write(f"{prefix}{slug}: {before} → public")
                if not dry_run:
                    skill.scope = SkillScope.PUBLIC
                    skill.organization = None
                    skill.save(update_fields=["scope", "organization", "updated_at"])
        if dry_run:
            self.stdout.write("Dry-run: no se escribió nada.")
