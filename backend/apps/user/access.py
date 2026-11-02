"""
Una persona de una organización sólo tiene acceso a su organización. Siempre.

Ecofilia tiene dos clases de usuarios con privilegios y no conviene
confundirlas:

- **El equipo de Ecofilia** no pertenece a ninguna organización. Es quien
  arma operaciones para los clientes, cura la biblioteca pública y asigna
  asistentes. ``is_staff``/``is_superuser``/``role=admin`` le dan alcance sobre
  toda la plataforma.
- **Un administrador de una organización** (``role=admin`` con
  ``organization``) manda dentro de su organización —por ejemplo, ejecuta
  asistentes en sus operaciones— pero nunca fuera de ella.

Hasta octubre de 2026 las dos cosas se medían con los mismos flags: un
``role=admin`` de CAF podía publicar en la biblioteca pública cualquier
documento de cualquier cliente, ver qué otros clientes tenían cada asistente,
y un miembro de una organización con ``is_staff`` veía todo. Este módulo es
el que separa una cosa de la otra.
"""
from __future__ import annotations


def belongs_to_organization(user) -> bool:
    return bool(getattr(user, "organization_id", None))


def is_organization_admin(user) -> bool:
    """Administrador de una institución: ``role=admin`` con organización.

    Dentro de su institución tiene sobre operaciones y documentos lo mismo que
    sus dueños —ver, editar, compartir, asignar, ejecutar, borrar—. Fuera de
    ella, nada; y lo que es de toda la plataforma (biblioteca pública,
    asignar asistentes a organizaciones) tampoco: eso es ``is_ecofilia_admin``.
    """
    from apps.user.models import UserRole

    return bool(
        getattr(user, "is_authenticated", False)
        and belongs_to_organization(user)
        and getattr(user, "role", None) == UserRole.ADMIN
    )


def administers_member(admin, member) -> bool:
    """Si ``admin`` administra la institución a la que pertenece ``member``."""
    return bool(
        member is not None
        and is_organization_admin(admin)
        and getattr(member, "organization_id", None) == admin.organization_id
    )


def is_ecofilia_admin(user) -> bool:
    """Superadmin de la plataforma: superusuario o ``role=admin``, sin organización.

    Es quien puede tocar lo que es de todos: publicar en la biblioteca
    pública, asignar asistentes a organizaciones, ver qué organización tiene
    qué.
    """
    from apps.user.models import UserRole

    return bool(
        getattr(user, "is_authenticated", False)
        and not belongs_to_organization(user)
        and not getattr(user, "_privileged_without_mfa", False)
        and (user.is_superuser or getattr(user, "role", None) == UserRole.ADMIN)
    )


def is_privileged(user) -> bool:
    """Si ``user`` tendría alcance de plataforma: staff, superusuario o
    ``role=admin``, sin institución. No mira el MFA."""
    from apps.user.models import UserRole

    return bool(
        getattr(user, "is_authenticated", False)
        and not belongs_to_organization(user)
        and (user.is_staff or user.is_superuser or getattr(user, "role", None) == UserRole.ADMIN)
    )


def require_mfa_for_privileges(user):
    """Le quita, sólo en memoria, el alcance de plataforma a quien no tiene MFA.

    Con ``ADMIN_REQUIRE_MFA``, un superadmin o un staff sin MFA activo es, para
    la API, un usuario común hasta que lo active: ve lo suyo y nada de las
    instituciones. Es lo mismo que hace ``scope_to_organization`` con un
    miembro marcado ``is_staff`` por error, y tampoco se persiste
    (``User.save`` vuelve a escribir los valores reales).
    """
    from django.conf import settings

    if user is None or not getattr(settings, "ADMIN_REQUIRE_MFA", True):
        return user
    if getattr(user, "mfa_enabled", False) or not is_privileged(user):
        return user
    if getattr(user, "_unscoped_flags", None) is None:
        user._unscoped_flags = (user.is_staff, user.is_superuser)
    user.is_staff = False
    user.is_superuser = False
    user._privileged_without_mfa = True
    return user


def home_path(user) -> str:
    """A dónde va ``user`` al entrar: la consola, su portal o el portal público."""
    if is_ecofilia_admin(user):
        return "/admin"
    if belongs_to_organization(user):
        return f"/{user.organization.slug}"
    return "/app"


def is_ecofilia_superuser(user) -> bool:
    """Superusuario de la plataforma, sin organización."""
    return bool(
        getattr(user, "is_authenticated", False)
        and not belongs_to_organization(user)
        and user.is_superuser
    )


def scope_to_organization(user):
    """Le quita, sólo en memoria, el alcance global a un miembro de una organización.

    ``is_staff`` e ``is_superuser`` abren la plataforma entera (hay decenas de
    chequeos que lo asumen). Un miembro de una organización no debería tener
    ninguno de los dos —el admin no deja guardarlo, ver ``User.clean``—, pero
    si un dato viejo los tiene, la API igual lo trata como un miembro más.

    No se persiste: ``User.save`` vuelve a escribir los valores reales, así que
    un usuario mal configurado se corrige desde el admin, a conciencia, y no
    por efecto secundario de que haya editado su perfil.
    """
    if user is None or not belongs_to_organization(user):
        return user
    if user.is_staff or user.is_superuser:
        user._unscoped_flags = (user.is_staff, user.is_superuser)
        user.is_staff = False
        user.is_superuser = False
    return user


PRIVILEGED_MFA_MESSAGE = (
    "Esta acción es de superadmin y exige MFA activo. Activalo desde tu perfil "
    "(POST /api/auth/mfa/setup/ y /api/auth/mfa/verify/) y volvé a entrar."
)


def privileged_mfa_missing(user) -> bool:
    """Si ``user`` actúa como superadmin sin el MFA que exige ``ADMIN_REQUIRE_MFA``.

    Con MFA activo el login JWT pide el código, así que un token suyo prueba
    los dos factores (la API ya no acepta Basic ni sesión).
    """
    from django.conf import settings

    return bool(getattr(settings, "ADMIN_REQUIRE_MFA", True) and not getattr(user, "mfa_enabled", False))


def ensure_privileged_mfa(user) -> None:
    """Corta con 403 una acción de superadmin hecha sin MFA."""
    if privileged_mfa_missing(user):
        from rest_framework.exceptions import PermissionDenied

        raise PermissionDenied(PRIVILEGED_MFA_MESSAGE, code="mfa_required")


def institution_boundary_q(user, field: str = "organization"):
    """Condición para que algo de una institución le llegue a ``user``.

    Lo de una institución lo ve sólo quien es de esa institución, aunque lo
    haya creado: quien sale de una institución deja de ver sus operaciones,
    sus corridas y sus chats. Lo que no es de ninguna institución no cambia.

    El superadmin no tiene institución y arma operaciones para todas: para él no
    hay límite (sin ampliarle nada: sigue viendo lo suyo y lo compartido).
    """
    from django.db.models import Q

    if is_ecofilia_admin(user):
        return Q()
    boundary = Q(**{f"{field}__isnull": True})
    if getattr(user, "organization_id", None):
        boundary |= Q(**{f"{field}_id": user.organization_id})
    return boundary


def within_institution(user, organization_id) -> bool:
    return (
        organization_id is None
        or organization_id == getattr(user, "organization_id", None)
        or is_ecofilia_admin(user)
    )


def public_reach_q(user, owner_field: str = "owner"):
    """Condición para que algo «público» le llegue a ``user``.

    «Público» quiere decir de la plataforma, no de una institución: lo que
    publica alguien de una institución no sale de ella. Sin esto, una
    evaluación (o un repositorio) que un miembro marcaba pública la veía
    cualquiera, de cualquier institución, con sus documentos y resultados.
    """
    from django.db.models import Q

    reach = Q(**{f"{owner_field}__isnull": True}) | Q(**{f"{owner_field}__organization__isnull": True})
    if getattr(user, "organization_id", None):
        reach |= Q(**{f"{owner_field}__organization_id": user.organization_id})
    return reach


def public_reaches(user, owner) -> bool:
    """Lo mismo que ``public_reach_q``, para un objeto ya cargado."""
    owner_org = getattr(owner, "organization_id", None) if owner is not None else None
    return owner_org is None or owner_org == getattr(user, "organization_id", None)
