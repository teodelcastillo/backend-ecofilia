"""
Siembra una base LOCAL con todo lo necesario para probar a mano el contrato de
aislamiento entre instituciones (fase 0) desde el frontend.

    python manage.py seed_local_instituciones
    python manage.py seed_local_instituciones --password otra-clave

Se niega a correr contra producción: con ``DEBUG`` falso, con settings de prod
o con una base en RDS corta sin tocar nada, y no hay forma de saltearlo.

Los usuarios, la clave y lo que debería ver cada uno están documentados en
``backend/docs/contrato-instituciones.md``: si cambia algo acá, cambia allá.

Deja la base como quedaría producción después de las migraciones de la fase 0:
crea la institución CAF y vuelve a aplicar las migraciones de datos
(``skill/0049`` y ``document/0024``), que en una base nueva corrieron sin CAF y
dejaron todo en plataforma/global. Además suma una segunda institución
(``otra``), un usuario público y un superadmin, con una operación, un documento
y un informe emitido cada uno, para que se vea qué ve cada nivel.

Es idempotente: correrlo dos veces no duplica nada.
"""
from __future__ import annotations

import importlib
import os

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument, ProjectStageReport, ProjectStageReportOrigin
from apps.project.services.stages import INSUMO_SLUG
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillScope, SkillType
from apps.user.models import Organization, UserRole

User = get_user_model()

DOMAIN = "local.test"


def _refuse_production() -> None:
    module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    host = str(settings.DATABASES["default"].get("HOST") or "")
    if (
        not settings.DEBUG
        or module.endswith(".prod")
        or "rds.amazonaws.com" in host
        or getattr(settings, "ENVIRONMENT_NAME", "") == "production"
    ):
        raise CommandError(
            "seed_local_instituciones es sólo para una base local con DEBUG=True. "
            f"DEBUG={settings.DEBUG}, settings={module or '?'}, host={host or '?'}: no se tocó nada."
        )


class Command(BaseCommand):
    help = "Siembra una base local para probar el contrato de aislamiento (nunca en producción)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="ecofilia-local", help="Clave de todos los usuarios sembrados.")

    def handle(self, *args, **options):
        _refuse_production()
        with transaction.atomic():
            self._seed(options["password"])

    # ------------------------------------------------------------------

    def _user(self, email, password, **extra):
        user, _ = User.objects.get_or_create(email=email, defaults={"username": email, **extra})
        for key, value in extra.items():
            setattr(user, key, value)
        user.email_verified = True
        user.approved = True
        user.set_password(password)
        user.save()
        return user

    def _skill(self, slug, scope, organization=None, owner=None):
        skill, _ = Skill.objects.update_or_create(
            slug=slug,
            defaults={
                "name": slug.replace("-", " ").capitalize(),
                "skill_type": SkillType.QUICK,
                "prompt_template": "Resumí: {{context}}",
                "allowed_contexts": ["project", "document"],
                "scope": scope,
                "organization": organization,
                "owner": owner,
                "members_can_run": True,
            },
        )
        return skill

    def _operation(self, owner, organization, name, doc_slug, tags=()):
        project, _ = Project.objects.get_or_create(
            slug=f"local-{doc_slug}",
            defaults={"owner": owner, "organization": organization, "name": name,
                      "context_notes": {"estado": "ido_cci", "pais": "Perú"}},
        )
        doc, _ = Document.objects.get_or_create(
            slug=doc_slug,
            defaults={"owner": owner, "name": doc_slug, "chunking_status": "done",
                      "extracted_text": f"Texto de prueba de {doc_slug}."},
        )
        doc.evidence_tags.add(*tags)
        ProjectDocument.objects.get_or_create(project=project, document=doc, defaults={"added_by": owner})
        if not ProjectStageReport.objects.filter(project=project, emitted_at__isnull=False).exists():
            ProjectStageReport.objects.create(
                project=project, stage="ido_cci", origin=ProjectStageReportOrigin.EDITADO,
                created_by=owner, emitted_by=owner, emitted_at=timezone.now(),
                emitted_markdown=f"# Informe emitido de {name}",
                emitted_assessment={"determinaciones": {"ct_a1": "Consistente"}},
            )
        return project

    def _seed(self, password):
        caf, _ = Organization.objects.get_or_create(slug="caf", defaults={"name": "CAF", "restricted": True})
        otra, _ = Organization.objects.get_or_create(slug="otra", defaults={"name": "Otra institución"})

        # Lo que en producción dejan las migraciones de la fase 0 con CAF presente.
        importlib.import_module("apps.skill.migrations.0049_asignar_alcance_skills").asignar(django_apps, None)
        importlib.import_module("apps.document.migrations.0024_etiquetas_de_operacion_caf").asignar(django_apps, None)
        caf_skills = Skill.objects.filter(scope=SkillScope.ORGANIZATION, organization=caf).exclude(slug=INSUMO_SLUG)
        caf.enabled_skills.add(*caf_skills)

        superadmin = self._user(f"superadmin@{DOMAIN}", password, is_superuser=True, is_staff=True)
        caf_admin = self._user(f"caf-admin@{DOMAIN}", password, organization=caf, role=UserRole.ADMIN)
        caf_member = self._user(f"caf-miembro@{DOMAIN}", password, organization=caf)
        otra_member = self._user(f"otra-miembro@{DOMAIN}", password, organization=otra)
        public = self._user(f"publico@{DOMAIN}", password)

        self._skill("local-publica", SkillScope.PUBLIC)
        self._skill("local-plataforma", SkillScope.PLATFORM)
        skill_otra = self._skill("local-de-otra", SkillScope.ORGANIZATION, organization=otra)
        self._skill("local-propia-del-publico", SkillScope.USER, owner=public)

        tag_otra, _ = EvidenceTag.objects.get_or_create(
            slug="propia-de-otra", organization=otra, defaults={"name": "Propia de otra"}
        )
        caf_tags = list(EvidenceTag.objects.filter(organization=caf, slug__in=["ido", "ias"]))
        ndc = list(EvidenceTag.objects.filter(organization__isnull=True, slug="ndc"))

        op_caf = self._operation(caf_member, caf, "Operación CAF (local)", "doc-caf", caf_tags + ndc)
        op_otra = self._operation(otra_member, otra, "Operación de otra (local)", "doc-otra", [tag_otra, *ndc])
        self._operation(public, None, "Operación del público (local)", "doc-publico", ndc)
        op_otra.enabled_skills.add(skill_otra)
        if not SkillExecution.objects.filter(project=op_otra).exists():
            SkillExecution.objects.create(
                skill=skill_otra, owner=otra_member, project=op_otra, status=ExecutionStatus.COMPLETED,
                output="Corrida de prueba de otra.",
            )
        op_caf.enabled_skills.add(*caf_skills)

        self._report(password, superadmin, caf_admin, caf_member, otra_member, public)

    def _report(self, password, *users):
        w = self.stdout.write
        w(self.style.SUCCESS("Base local sembrada. Todos los usuarios tienen la clave: " + password))
        w("")
        w("Usuario                         Nivel                     Debería ver")
        rows = [
            (users[0], "superadmin", "todo: las 3 operaciones, todas las skills y etiquetas"),
            (users[1], "admin de CAF", "operaciones de CAF; skills públicas y caf-* habilitadas; etiquetas CAF + globales"),
            (users[2], "miembro de CAF", "su operación; skills públicas y caf-* habilitadas; etiquetas CAF + globales"),
            (users[3], "miembro de otra", "su operación; local-publica y local-de-otra; propia-de-otra + globales"),
            (users[4], "público", "su operación; local-publica y la suya; sólo etiquetas globales"),
        ]
        for user, level, sees in rows:
            w(f"{user.email:<31} {level:<25} {sees}")
        w("")
        w("Nadie salvo el superadmin ve local-plataforma. evaluation-records exige ?organization=<slug>.")
