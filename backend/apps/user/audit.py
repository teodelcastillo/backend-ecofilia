"""
El registro de auditoría (``AuditEvent``).

``record`` se llama desde cada acción privilegiada y desde la autenticación.
Dentro de una transacción, el evento entra o sale con la acción: no queda una
acción sin rastro ni un rastro de algo que no pasó.

Qué se registra (``action``):
- auth.login.success · auth.login.failure · auth.account.locked
- auth.password.reset · auth.password.change · auth.mfa.enabled · auth.mfa.disabled
- admin.organization.created · admin.organization.updated · admin.member.added
- admin.user.updated · skill.scope.changed · skill.version.informed_by
- config.organization.updated · config.platform.updated
- tag.created · tag.updated · document.visibility.changed · evaluation.visibility.changed
"""
from __future__ import annotations

from apps.user.models import AuditEvent


def client_ip(request) -> str | None:
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    # Detrás del ALB, la primera dirección es la del cliente.
    ip = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
    return ip or None


def record(action: str, *, request=None, actor=None, organization=None, target=None, data=None) -> AuditEvent:
    if actor is None and request is not None and getattr(request.user, "is_authenticated", False):
        actor = request.user
    target_type = target_id = ""
    if target is not None:
        target_type = target._meta.label_lower
        target_id = str(getattr(target, "slug", None) or target.pk)
    return AuditEvent.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        actor_email=(getattr(actor, "email", "") or "")[:254],
        action=action,
        organization=organization,
        target_type=target_type,
        target_id=target_id[:255],
        data=data or {},
        ip=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT", "") if request is not None else "")[:255],
    )
