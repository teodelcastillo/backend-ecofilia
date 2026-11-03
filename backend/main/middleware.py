class HealthCheckMiddleware:
    """Allow ALB health checks to bypass ALLOWED_HOSTS validation.

    ECS ALB sends health checks with the task's private IP as Host header,
    which Django rejects. This middleware intercepts /health/ before the
    security middleware runs and delegates to the health view directly.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/health/":
            from main.health import health_check

            return health_check(request)
        return self.get_response(request)


class PrivilegedAccessAuditMiddleware:
    """Deja en la auditoría cada pedido a la API de un superadmin o del staff.

    Lo que lee el superadmin es lo primero que pide revisar una institución: él
    ve las operaciones, los documentos y los informes de todas. Las escrituras
    privilegiadas, además, tienen su propio evento con el antes y el después
    (``apps.user.audit``); esto es el rastro de acceso, lecturas incluidas.

    Va después de la respuesta porque el usuario de la API lo resuelve DRF (JWT)
    dentro de la vista, y DRF lo deja en el ``request`` de Django.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._record(request, response)
        except Exception:  # noqa: BLE001 — el registro de acceso no puede tirar el pedido
            import logging

            logging.getLogger(__name__).exception("No se pudo registrar el acceso privilegiado")
        return response

    def _record(self, request, response):
        from django.conf import settings

        if not getattr(settings, "AUDIT_PRIVILEGED_ACCESS", True) or not request.path.startswith("/api/"):
            return
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            return
        from apps.user.access import is_ecofilia_admin

        if not (user.is_staff or is_ecofilia_admin(user)):
            return
        from apps.user import audit
        from apps.user.models import Organization

        slug = request.GET.get("organization") or ""
        parts = request.path.strip("/").split("/")
        if not slug and len(parts) >= 3 and parts[1] in ("organizations",) and parts[2] not in ("me", ""):
            slug = parts[2]
        organization = Organization.objects.filter(slug=slug).first() if slug else None
        audit.record(
            "superadmin.access",
            request=request,
            actor=user,
            organization=organization,
            data={
                "method": request.method,
                "path": request.path[:500],
                "query": request.META.get("QUERY_STRING", "")[:500],
                "status": response.status_code,
            },
        )
