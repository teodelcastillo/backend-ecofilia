"""
La consola del superadmin (fase 2) y el alta de instituciones (fase 5).

Todo lo de ``/api/admin/`` es sólo del superadmin de Ecofilia
(``is_ecofilia_admin``); cualquier otro recibe 403. Contrato en
``backend/docs/contrato-instituciones.md``.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.user.access import is_ecofilia_admin
from apps.user.models import KNOWN_FEATURES, Organization, UserRole

User = get_user_model()


class IsEcofiliaAdmin(BasePermission):
    message = "La consola es sólo del superadmin de Ecofilia."

    def has_permission(self, request, view):
        return is_ecofilia_admin(request.user)


class AdminView(APIView):
    permission_classes = [IsAuthenticated, IsEcofiliaAdmin]


def _bad(errors) -> Response:
    return Response(errors, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Instituciones
# ---------------------------------------------------------------------------

def _organizations_queryset():
    from apps.document.models import EvidenceTag
    from apps.project.models import Project
    from apps.skill.models import Skill, SkillScope

    def count(model, **filters):
        from django.db.models import IntegerField, OuterRef, Subquery
        from django.db.models.functions import Coalesce

        return Coalesce(
            Subquery(
                model.objects.filter(organization=OuterRef("pk"), **filters)
                .values("organization")
                .annotate(n=Count("pk"))
                .values("n")[:1],
                output_field=IntegerField(),
            ),
            0,
        )

    return Organization.objects.annotate(
        members_count=count(User),
        operations_count=count(Project),
        own_skills_count=count(Skill, scope=SkillScope.ORGANIZATION),
        own_tags_count=count(EvidenceTag),
    ).order_by("name")


def organization_item(org) -> dict:
    return {
        "slug": org.slug,
        "name": org.name,
        "restricted": org.restricted,
        "has_config": bool(org.config),
        "members_count": getattr(org, "members_count", None),
        "operations_count": getattr(org, "operations_count", None),
        "own_skills_count": getattr(org, "own_skills_count", None),
        "own_tags_count": getattr(org, "own_tags_count", None),
        "created_at": org.created_at,
    }


def organization_detail(org) -> dict:
    org = _organizations_queryset().get(pk=org.pk)
    return {
        **organization_item(org),
        "enabled_features": list(org.enabled_features or []),
        "enabled_skill_slugs": sorted(org.enabled_skills.values_list("slug", flat=True)),
        "default_skill_slugs": sorted(org.default_project_skills.values_list("slug", flat=True)),
    }


def _usable_skills(org, slugs, field, errors):
    """Las skills que la institución puede tener asignadas: públicas o suyas."""
    from apps.skill.models import Skill, SkillScope

    if not isinstance(slugs, list) or not all(isinstance(s, str) for s in slugs):
        errors[field] = ["Tiene que ser una lista de slugs."]
        return []
    found = {
        s.slug: s
        for s in Skill.objects.filter(slug__in=slugs).filter(
            Q(scope=SkillScope.PUBLIC) | Q(scope=SkillScope.ORGANIZATION, organization=org)
        )
    }
    missing = [s for s in dict.fromkeys(slugs) if s not in found]
    if missing:
        errors[field] = [f"No existen, o no son públicas ni de esta institución: {', '.join(missing)}."]
    return [found[s] for s in dict.fromkeys(slugs) if s in found]


class AdminOverviewView(AdminView):
    def get(self, request):
        from apps.skill.models import Skill, SkillScope

        levels = {"superadmin": 0, "organization": 0, "public": 0}
        for user in User.objects.only("is_superuser", "role", "organization_id", "is_active"):
            levels[user_level(user)] += 1
        by_scope = dict(Skill.objects.values_list("scope").annotate(n=Count("pk")))
        return Response(
            {
                "levels": levels,
                "skills_by_scope": {scope: by_scope.get(scope, 0) for scope in SkillScope.values},
                "organizations": [organization_item(o) for o in _organizations_queryset()],
            }
        )


class AdminOrganizationsView(AdminView):
    def get(self, request):
        return Response([organization_item(o) for o in _organizations_queryset()])

    def post(self, request):
        from apps.user.onboarding import OnboardingError, create_institution

        try:
            result = create_institution(request.data if isinstance(request.data, dict) else {})
        except OnboardingError as exc:
            return _bad(exc.errors)
        return Response(
            {
                **organization_detail(result.organization),
                "cloned_skills": result.cloned_skills,
                "members": result.members,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminOrganizationDetailView(AdminView):
    def get(self, request, slug):
        return Response(organization_detail(get_object_or_404(Organization, slug=slug)))

    def patch(self, request, slug):
        org = get_object_or_404(Organization, slug=slug)
        data = request.data if isinstance(request.data, dict) else {}
        errors: dict[str, list[str]] = {}
        unknown = sorted(
            set(data) - {"name", "restricted", "enabled_features", "enabled_skill_slugs", "default_skill_slugs"}
        )
        if unknown:
            errors["detail"] = [f"Campos que no se editan acá: {', '.join(unknown)}."]

        if "name" in data:
            name = str(data.get("name") or "").strip()
            if not name:
                errors["name"] = ["No puede estar vacío."]
            elif Organization.objects.filter(name=name).exclude(pk=org.pk).exists():
                errors["name"] = [f"Ya existe una institución «{name}»."]
        if "restricted" in data and not isinstance(data.get("restricted"), bool):
            errors["restricted"] = ["Tiene que ser true o false."]
        if "enabled_features" in data:
            features = data.get("enabled_features")
            if not isinstance(features, list):
                errors["enabled_features"] = ["Tiene que ser una lista."]
            else:
                unknown_features = [f for f in features if f not in KNOWN_FEATURES]
                if unknown_features:
                    errors["enabled_features"] = [
                        f"Secciones desconocidas: {', '.join(map(str, unknown_features))}. "
                        f"Conocidas: {', '.join(KNOWN_FEATURES)}."
                    ]
        enabled = default = None
        if "enabled_skill_slugs" in data:
            enabled = _usable_skills(org, data.get("enabled_skill_slugs"), "enabled_skill_slugs", errors)
        if "default_skill_slugs" in data:
            default = _usable_skills(org, data.get("default_skill_slugs"), "default_skill_slugs", errors)
            enabled_slugs = (
                {s.slug for s in enabled} if enabled is not None
                else set(org.enabled_skills.values_list("slug", flat=True))
            )
            outside = sorted({s.slug for s in default} - enabled_slugs)
            if outside and "default_skill_slugs" not in errors:
                errors["default_skill_slugs"] = [
                    f"Tienen que estar también habilitadas: {', '.join(outside)}."
                ]
        if errors:
            return _bad(errors)

        with transaction.atomic():
            fields = []
            if "name" in data:
                org.name = str(data["name"]).strip()
                fields.append("name")
            if "restricted" in data:
                org.restricted = data["restricted"]
                fields.append("restricted")
            if "enabled_features" in data:
                org.enabled_features = list(dict.fromkeys(data["enabled_features"]))
                fields.append("enabled_features")
            if fields:
                org.save(update_fields=[*fields, "updated_at"])
            if enabled is not None:
                org.enabled_skills.set(enabled)
            if default is not None:
                org.default_project_skills.set(default)
        return Response(organization_detail(org))


class AdminOrganizationMembersView(AdminView):
    def post(self, request, slug):
        from apps.user.onboarding import OnboardingError, add_member

        org = get_object_or_404(Organization, slug=slug)
        data = request.data if isinstance(request.data, dict) else {}
        try:
            with transaction.atomic():
                member = add_member(org, data.get("email"), data.get("role") or UserRole.MEMBER)
        except OnboardingError as exc:
            return _bad(exc.errors)
        return Response(member, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Usuarios
# ---------------------------------------------------------------------------

def user_level(user) -> str:
    if is_ecofilia_admin(user):
        return "superadmin"
    if user.organization_id:
        return "organization"
    return "public"


def user_item(user) -> dict:
    return {
        "id": user.pk,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "level": user_level(user),
        "organization_slug": user.organization.slug if user.organization_id else None,
        "role": user.role,
        "is_active": user.is_active,
        "email_verified": user.email_verified,
        "last_login": user.last_login,
        "date_joined": user.date_joined,
        "operations_count": getattr(user, "operations_count", None),
    }


def _users_queryset():
    return User.objects.select_related("organization").annotate(
        operations_count=Count("projects", distinct=True)
    ).order_by("email")


class AdminUsersView(AdminView):
    def get(self, request):
        qs = _users_queryset()
        if org := request.query_params.get("organization"):
            qs = qs.filter(organization__slug=org)
        if text := (request.query_params.get("q") or "").strip():
            qs = qs.filter(
                Q(email__icontains=text) | Q(first_name__icontains=text) | Q(last_name__icontains=text)
            )
        users = list(qs)
        if level := request.query_params.get("level"):
            if level not in ("superadmin", "organization", "public"):
                return _bad({"level": ["Tiene que ser superadmin, organization o public."]})
            users = [u for u in users if user_level(u) == level]
        return Response([user_item(u) for u in users])


class AdminUserDetailView(AdminView):
    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        data = request.data if isinstance(request.data, dict) else {}
        errors: dict[str, list[str]] = {}
        unknown = sorted(set(data) - {"organization_slug", "role", "is_active"})
        if unknown:
            errors["detail"] = [f"Campos que no se editan acá: {', '.join(unknown)}."]
        if user.pk == request.user.pk and set(data) & {"organization_slug", "role", "is_active"}:
            errors["detail"] = [
                "No podés cambiar tu propia institución, rol o estado: te quitarías el acceso por error."
            ]

        organization = user.organization
        if "organization_slug" in data:
            slug = data.get("organization_slug")
            if slug in (None, ""):
                organization = None
            else:
                organization = Organization.objects.filter(slug=slug).first()
                if organization is None:
                    errors["organization_slug"] = [f"No existe la institución «{slug}»."]
                elif user.is_staff or user.is_superuser:
                    errors["organization_slug"] = [
                        "Un usuario staff o superusuario no puede tener institución: "
                        "tendría acceso a todas las instituciones."
                    ]
        if "role" in data and data.get("role") not in UserRole.values:
            errors["role"] = [f"Tiene que ser uno de {', '.join(UserRole.values)}."]
        if "is_active" in data and not isinstance(data.get("is_active"), bool):
            errors["is_active"] = ["Tiene que ser true o false."]
        # ``role=admin`` sin institución es superadmin de toda la plataforma
        # (``is_ecofilia_admin``): sacarle la institución a un administrador de
        # institución, o darle ``admin`` a un público, lo ascendería sin querer.
        # La consola no crea superadmins; eso se decide a conciencia en el admin.
        role = data.get("role", user.role)
        if (
            not errors
            and not is_ecofilia_admin(user)
            and organization is None
            and role == UserRole.ADMIN
            and not user.is_superuser
        ):
            errors["detail"] = [
                "Así quedaría como superadmin de toda la plataforma (role=admin sin institución). "
                "La consola no crea superadmins: cambiale el rol en el mismo pedido."
            ]
        if errors:
            return _bad(errors)

        fields = []
        if "organization_slug" in data:
            user.organization = organization
            fields.append("organization")
        if "role" in data:
            user.role = data["role"]
            fields.append("role")
        if "is_active" in data:
            user.is_active = data["is_active"]
            fields.append("is_active")
        if fields:
            user.save(update_fields=fields)
        return Response(user_item(_users_queryset().get(pk=user.pk)))


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class AdminSkillScopeView(AdminView):
    """Cambio de alcance en lote: lo de ``listar_skills_alcance --hacer-publicas``."""

    def post(self, request):
        from apps.skill.models import Skill, SkillScope

        data = request.data if isinstance(request.data, dict) else {}
        slugs = data.get("slugs")
        scope = data.get("scope")
        errors: dict[str, list[str]] = {}
        if not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) for s in slugs):
            errors["slugs"] = ["Tiene que ser una lista de slugs, con al menos uno."]
            slugs = []
        if scope not in SkillScope.values:
            errors["scope"] = [f"Tiene que ser uno de {', '.join(SkillScope.values)}."]
        organization = None
        if scope == SkillScope.ORGANIZATION:
            org_slug = data.get("organization_slug")
            organization = Organization.objects.filter(slug=org_slug).first() if org_slug else None
            if organization is None:
                errors["organization_slug"] = ["Un alcance institución necesita una institución que exista."]
        found = {s.slug: s for s in Skill.objects.filter(slug__in=slugs)}
        missing = [s for s in dict.fromkeys(slugs) if s not in found]
        if missing:
            errors["slugs"] = [f"No existen: {', '.join(missing)}. No se cambió ninguna."]
        if errors:
            return _bad(errors)

        with transaction.atomic():
            for slug in dict.fromkeys(slugs):
                skill = found[slug]
                skill.scope = scope
                skill.organization = organization
                skill.save(update_fields=["scope", "organization", "updated_at"])
        return Response(
            [
                {
                    "slug": found[s].slug,
                    "scope": found[s].scope,
                    "organization_slug": organization.slug if organization else None,
                }
                for s in dict.fromkeys(slugs)
            ]
        )
