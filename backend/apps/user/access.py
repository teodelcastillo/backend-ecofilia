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
        and (user.is_superuser or getattr(user, "role", None) == UserRole.ADMIN)
    )


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
