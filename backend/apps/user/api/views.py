"""
La configuración de cada institución (fase 1). Contrato en
``backend/docs/contrato-instituciones.md``.

- ``GET /api/organizations/me/config/``: la de la institución de quien pide,
  o la base si no tiene.
- ``GET /api/organizations/{slug}/config/``: la de una institución, para sus
  miembros y el superadmin. Para el resto no existe (404): ni siquiera se
  revela que la institución existe.
- ``PUT /api/organizations/{slug}/config/``: la reemplaza entera. Sólo
  superadmin; inválida, 400 con todos los errores.
- ``GET /api/organizations/`` (fase 3): los portales que puede abrir quien
  pide. El superadmin, todos; un miembro, el suyo; el público, ninguno.
"""
from __future__ import annotations

from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.user import institution_config
from apps.user.access import is_ecofilia_admin
from apps.user.models import Organization, PlatformConfig


def config_payload(organization) -> dict:
    return {
        "organization": (
            {"slug": organization.slug, "name": organization.name, "restricted": organization.restricted}
            if organization is not None
            else None
        ),
        "config": institution_config.effective_config(organization),
    }


class PortalsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if is_ecofilia_admin(user):
            organizations = Organization.objects.order_by("name")
        elif user.organization_id:
            organizations = Organization.objects.filter(pk=user.organization_id)
        else:
            organizations = Organization.objects.none()
        out = []
        for org in organizations:
            branding = institution_config.effective_config(org)["branding"]
            out.append(
                {
                    "slug": org.slug,
                    "name": org.name,
                    "restricted": org.restricted,
                    "display_name": branding["display_name"],
                    "logo_url": branding["logo_url"],
                }
            )
        return Response(out)


class MyOrganizationConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        organization = request.user.organization if request.user.organization_id else None
        return Response(config_payload(organization))


class OrganizationConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def _organization(self, request, slug: str) -> Organization:
        user = request.user
        organization = Organization.objects.filter(slug=slug).first()
        if organization is None:
            raise Http404
        if is_ecofilia_admin(user) or organization.pk == getattr(user, "organization_id", None):
            return organization
        raise Http404

    def get(self, request, slug: str):
        return Response(config_payload(self._organization(request, slug)))

    def put(self, request, slug: str):
        if not is_ecofilia_admin(request.user):
            # Un miembro sabe que su institución existe; al resto, 404 igual que el GET.
            self._organization(request, slug)
            raise PermissionDenied("Sólo un superadmin cambia la configuración de una institución.")
        organization = self._organization(request, slug)
        raw = request.data.get("config") if isinstance(request.data, dict) else None
        config, errors = institution_config.validate_config(raw, organization)
        if errors:
            return Response({"config": errors}, status=status.HTTP_400_BAD_REQUEST)
        organization.config = config
        organization.save(update_fields=["config", "updated_at"])
        return Response(config_payload(organization))


class PlatformConfigView(APIView):
    """``/api/platform/config/{public|template}/`` (fase 4).

    La pública la lee cualquiera autenticado; la plantilla, sólo el superadmin.
    Escribir cualquiera de las dos es del superadmin, y se validan como «sin
    institución»: sólo skills públicas y etiquetas globales.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, key: str):
        if key != PlatformConfig.Key.PUBLIC and not is_ecofilia_admin(request.user):
            raise PermissionDenied("La plantilla de institución nueva es sólo del superadmin.")
        return Response({"config": institution_config.platform_config(key)})

    def put(self, request, key: str):
        if not is_ecofilia_admin(request.user):
            raise PermissionDenied("Sólo un superadmin cambia las configuraciones de la plataforma.")
        raw = request.data.get("config") if isinstance(request.data, dict) else None
        config, errors = institution_config.validate_config(raw, None)
        if errors:
            return Response({"config": errors}, status=status.HTTP_400_BAD_REQUEST)
        PlatformConfig.objects.update_or_create(key=key, defaults={"config": config})
        return Response({"config": institution_config.platform_config(key)})
