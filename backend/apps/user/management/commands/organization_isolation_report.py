"""
Qué datos de hoy contradicen la regla «una persona de una organización sólo
tiene acceso a su organización» (ver ``apps/user/access.py``).

Las reglas nuevas impiden crear estos casos, pero no borran los que ya
existen. Este comando los lista:

1. Miembros de una organización con ``is_staff`` o ``is_superuser``. La API ya
   los trata como miembros comunes; el comando no los toca porque no puede
   saber cuál de las dos cosas sobra —si es alguien de Ecofilia con una
   organización de prueba, o alguien del cliente con permisos de más—. Se
   corrigen desde el admin.
2. Accesos compartidos que cruzan organizaciones (operaciones, documentos,
   repositorios, evaluaciones): entre dueño e invitado, o entre la
   institución de la operación y el invitado. Con ``--revoke-shares`` se
   borran.
3. Operaciones sin institución que alguna institución reclama (dueño sin
   organización pero con asistentes de más de una): se asignan desde el admin.

    python manage.py organization_isolation_report
    python manage.py organization_isolation_report --revoke-shares
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.document.models import DocumentShare
from apps.evaluation.models import EvaluationShare
from apps.project.models import Project, ProjectShare
from apps.repository.models import RepositoryShare
from apps.user.models import Organization
from apps.user.sharing import belongs_to_resource_organization, within_organization_boundary

SHARE_SOURCES = (
    ("operación", ProjectShare, "project"),
    ("documento", DocumentShare, "document"),
    ("repositorio", RepositoryShare, "repository"),
    ("evaluación", EvaluationShare, "evaluation"),
)


def _org(user):
    return user.organization.slug if user and user.organization_id else "sin organización"


class Command(BaseCommand):
    help = "Lista (y opcionalmente revoca) lo que cruza los límites de una organización."

    def add_arguments(self, parser):
        parser.add_argument(
            "--revoke-shares",
            action="store_true",
            help="Borra los accesos compartidos que cruzan organizaciones.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        flagged = (
            User.objects.filter(organization__isnull=False)
            .filter(Q(is_staff=True) | Q(is_superuser=True))
            .select_related("organization")
            .order_by("organization__slug", "email")
        )
        self.stdout.write("Miembros de una organización con staff/superusuario:")
        for user in flagged:
            flags = [name for name, on in (("staff", user.is_staff), ("superusuario", user.is_superuser)) if on]
            self.stdout.write(f"  - {user.email} ({_org(user)}): {', '.join(flags)}")
        if not flagged:
            self.stdout.write("  ninguno")

        crossing = []
        for label, model, field in SHARE_SOURCES:
            shares = model.objects.select_related(
                "user__organization", f"{field}__owner__organization"
            )
            if model is ProjectShare:
                shares = shares.select_related("project__organization")
            for share in shares:
                resource = getattr(share, field)
                if not within_organization_boundary(
                    resource.owner, share.user
                ) or not belongs_to_resource_organization(
                    share.user, getattr(resource, "organization_id", None)
                ):
                    crossing.append((label, share, resource))

        self.stdout.write("\nAccesos compartidos que cruzan organizaciones:")
        for label, share, resource in crossing:
            owner = resource.owner
            self.stdout.write(
                f"  - {label} «{resource}» de {owner.email} ({_org(owner)}) "
                f"→ {share.user.email} ({_org(share.user)}), {share.role}"
            )
        if not crossing:
            self.stdout.write("  ninguno")

        self.stdout.write("\nOperaciones sin institución que más de una institución reclama:")
        ambiguous = 0
        for project in Project.objects.filter(organization__isnull=True).prefetch_related("enabled_skills"):
            orgs = sorted(
                Organization.objects.filter(enabled_skills__in=project.enabled_skills.all())
                .values_list("slug", flat=True)
                .distinct()
            )
            if len(orgs) > 1:
                ambiguous += 1
                self.stdout.write(f"  - «{project.name}» ({project.slug}): {', '.join(orgs)}")
        if not ambiguous:
            self.stdout.write("  ninguna")

        if options["revoke_shares"] and crossing:
            for _, share, _ in crossing:
                share.delete()
            self.stdout.write(self.style.WARNING(f"\nRevocados {len(crossing)} accesos."))
        elif crossing:
            self.stdout.write("\nPara revocarlos: --revoke-shares")
