"""
El alta de instituciones (fase 5). Contrato en ``backend/docs/contrato-instituciones.md``.

Dar de alta una institución es una sola decisión, así que se hace todo o nada:
1. se crea la institución;
2. se copian las skills de Ecofilia que pidió el superadmin;
3. se arma su configuración desde la plantilla (fase 4);
4. se suman sus miembros.

Si algo no cierra, no queda nada a medias. Los emails de invitación salen
recién cuando el alta se confirma.

Las copias sólo pueden salir de skills de Ecofilia (``public`` o ``platform``),
nunca de otra institución: copiar un workflow de CAF sería darle a otra
institución información de CAF (fase 0).
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.user import institution_config
from apps.user.access import is_ecofilia_admin
from apps.user.models import KNOWN_FEATURES, Organization, PlatformConfig, UserRole

User = get_user_model()

# Chocan con rutas del front (``/{slug}`` es el portal de la institución) o de la API.
RESERVED_SLUGS = frozenset(
    {
        "admin", "api", "app", "auth", "base", "demo", "evaluations", "forgot-password",
        "home", "library", "login", "me", "my-library", "platform", "projects",
        "reset-password", "signup",
    }
)

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class OnboardingError(Exception):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__(errors)
        self.errors = errors


@dataclass
class OnboardingResult:
    organization: Organization
    cloned_skills: list[dict] = field(default_factory=list)
    members: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Alta
# ---------------------------------------------------------------------------

def create_institution(data: dict) -> OnboardingResult:
    from apps.skill.models import Skill, SkillScope

    errors: dict[str, list[str]] = {}
    known = {"name", "slug", "restricted", "enabled_features", "from_template", "clone_skills", "members"}
    unknown = sorted(set(data) - known)
    if unknown:
        errors["detail"] = [f"Campos desconocidos: {', '.join(unknown)}."]

    name = str(data.get("name") or "").strip()
    if not name:
        errors["name"] = ["Es obligatorio."]
    elif Organization.objects.filter(name=name).exists():
        errors["name"] = [f"Ya existe una institución «{name}»."]

    slug = str(data.get("slug") or "").strip()
    if not _SLUG.match(slug) or len(slug) > 64:
        errors["slug"] = ["Tiene que ser minúsculas, números y guiones (hasta 64)."]
    elif slug in RESERVED_SLUGS:
        errors["slug"] = [f"«{slug}» está reservado: choca con una ruta del portal o de la API."]
    elif Organization.objects.filter(slug=slug).exists():
        errors["slug"] = [f"Ya existe una institución con el slug «{slug}»."]

    restricted = data.get("restricted", False)
    if not isinstance(restricted, bool):
        errors["restricted"] = ["Tiene que ser true o false."]
    features = data.get("enabled_features", [])
    if not isinstance(features, list) or any(f not in KNOWN_FEATURES for f in features):
        errors["enabled_features"] = [f"Tiene que ser una lista de: {', '.join(KNOWN_FEATURES)}."]
    from_template = data.get("from_template", True)
    if not isinstance(from_template, bool):
        errors["from_template"] = ["Tiene que ser true o false."]

    clone_slugs = data.get("clone_skills", [])
    sources: list = []
    if not isinstance(clone_slugs, list) or not all(isinstance(s, str) for s in clone_slugs):
        errors["clone_skills"] = ["Tiene que ser una lista de slugs."]
    else:
        clone_slugs = list(dict.fromkeys(clone_slugs))
        found = {s.slug: s for s in Skill.objects.filter(slug__in=clone_slugs)}
        missing = [s for s in clone_slugs if s not in found]
        foreign = [
            s for s in clone_slugs
            if s in found and found[s].scope not in (SkillScope.PUBLIC, SkillScope.PLATFORM)
        ]
        if missing:
            errors["clone_skills"] = [f"No existen: {', '.join(missing)}."]
        elif foreign:
            errors["clone_skills"] = [
                "Sólo se copian skills de Ecofilia (públicas o de plataforma); estas son de una "
                f"institución o de un usuario: {', '.join(foreign)}."
            ]
        sources = [found[s] for s in clone_slugs if s in found]

    members = data.get("members", [])
    if not isinstance(members, list) or not all(isinstance(m, dict) for m in members):
        errors["members"] = ['Tiene que ser una lista de {"email", "role"}.']
        members = []
    member_errors = [e for m in members for e in _member_problems(None, m.get("email"), m.get("role"))]
    if member_errors:
        errors["members"] = member_errors
    if errors:
        raise OnboardingError(errors)

    with transaction.atomic():
        org = Organization.objects.create(
            name=name, slug=slug, restricted=restricted, enabled_features=list(dict.fromkeys(features))
        )
        clones = [(source, clone_skill(source, org)) for source in sources]
        renames = {source.slug: clone.slug for source, clone in clones}

        if from_template:
            config = _remap(institution_config.platform_config(PlatformConfig.Key.TEMPLATE), renames)
            # La marca de la plantilla es un ejemplo: la institución lleva su nombre.
            config["branding"]["display_name"] = name
            clean, config_errors = institution_config.validate_config(config, org)
            if config_errors:
                # La excepción deshace el alta entera: no queda la institución a medias.
                raise OnboardingError({"config": config_errors})
            org.config = clean
            org.save(update_fields=["config", "updated_at"])

        for _, clone in clones:
            org.enabled_skills.add(clone)
            if set(clone.allowed_contexts or []) & {"project", "any"}:
                org.default_project_skills.add(clone)

        added = [add_member(org, m.get("email"), m.get("role") or UserRole.MEMBER) for m in members]

    return OnboardingResult(
        organization=org,
        cloned_skills=[{"from": source.slug, "slug": clone.slug} for source, clone in clones],
        members=added,
    )


def _remap(config: dict, renames: dict[str, str]) -> dict:
    """Si la plantilla nombra una skill que se copió, apunta a la copia."""
    config = copy.deepcopy(config)
    for stage in config.get("stages") or []:
        stage["launches"] = [renames.get(s, s) for s in stage.get("launches") or []]
    workflows = config.get("workflows") or {}
    for role, slug in list(workflows.items()):
        if slug:
            workflows[role] = renames.get(slug, slug)
    return config


# ---------------------------------------------------------------------------
# Copias de skills
# ---------------------------------------------------------------------------

def _clone_slug(org: Organization, source_slug: str) -> str:
    from apps.skill.models import Skill

    base = f"{org.slug}-{source_slug}"[:255]
    slug, counter = base, 1
    while Skill.objects.filter(slug=slug).exists():
        suffix = f"-{counter}"
        slug = f"{base[:255 - len(suffix)]}{suffix}"
        counter += 1
    return slug


def clone_skill(source, org: Organization):
    """Una copia propia de ``source`` para ``org``, con sus pasos y parámetros.

    No copia corridas, versiones ni asignaciones: la copia arranca su historia.
    """
    from apps.skill.models import Skill, SkillScope

    steps = list(source.steps.all())
    parameters = list(source.parameters.all())

    clone = Skill.objects.get(pk=source.pk)
    clone.pk = None
    clone.id = None
    clone._state.adding = True
    clone.slug = _clone_slug(org, source.slug)
    clone.scope = SkillScope.ORGANIZATION
    clone.organization = org
    clone.owner = None
    clone.is_template = False
    clone.is_default_enabled = False
    clone.save()

    for item in steps + parameters:
        item.pk = None
        item.id = None
        item._state.adding = True
        item.skill = clone
        item.save()
    return clone


# ---------------------------------------------------------------------------
# Miembros
# ---------------------------------------------------------------------------

def _normalize_email(email) -> str:
    return str(email or "").strip().lower()


def _member_problems(org, email, role) -> list[str]:
    email = _normalize_email(email)
    if not email or "@" not in email:
        return [f"«{email}»: no es un email válido."]
    if role not in (None, "", *UserRole.values):
        return [f"{email}: el rol tiene que ser uno de {', '.join(UserRole.values)}."]
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        return []
    if user.is_staff or user.is_superuser or is_ecofilia_admin(user):
        return [f"{email}: es del equipo de Ecofilia; no puede pertenecer a una institución."]
    if user.organization_id and (org is None or user.organization_id != org.pk):
        return [f"{email}: ya pertenece a otra institución."]
    return []


def add_member(org: Organization, email, role) -> dict:
    """Suma ``email`` a ``org``: lo crea e invita, lo pasa a la institución, o reinvita.

    - No existe: se crea aprobado, sin contraseña y con el email sin verificar,
      y se le manda la invitación.
    - Existe sin institución: pasa a ``org`` con ``role``.
    - Ya es de ``org``: se le actualiza el rol y, si todavía no verificó su
      email, se le reenvía la invitación.
    """
    problems = _member_problems(org, email, role)
    if problems:
        raise OnboardingError({"members": problems})
    email = _normalize_email(email)
    role = role or UserRole.MEMBER

    user = User.objects.filter(email__iexact=email).first()
    created = user is None
    if created:
        user = User.objects.create_user(email=email, password=None, organization=org, role=role, approved=True)
    else:
        user.organization = org
        user.role = role
        user.save(update_fields=["organization", "role"])

    invited = not user.email_verified
    if invited:
        transaction.on_commit(lambda: send_invitation(user.pk, org.pk))
    return {"email": user.email, "id": user.pk, "created": created, "invited": invited}


def send_invitation(user_id: int, org_id: int) -> None:
    import logging

    from apps.authentication.api.views import build_frontend_url
    from apps.authentication.services.email import EmailService
    from apps.authentication.services.tokens import TokenService

    user = User.objects.get(pk=user_id)
    org = Organization.objects.get(pk=org_id)
    url = build_frontend_url(
        "/auth/reset-password",
        {"uid": TokenService.make_uid(user), "token": TokenService.generate_token(user), "invite": "1"},
    )
    try:
        EmailService.send_invitation_email(user, org, url)
    except Exception:  # noqa: BLE001 — el alta ya está hecha; reenviar es volver a agregarlo.
        logging.getLogger(__name__).exception("No se pudo mandar la invitación a %s", user.email)
