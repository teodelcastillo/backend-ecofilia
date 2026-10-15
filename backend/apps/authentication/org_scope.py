"""
Autenticación de la API con el alcance de la organización aplicado.

Las tres formas de autenticarse contra la API pasan por acá, así que todo
pedido de un miembro de una organización llega a las vistas sin alcance
global (ver ``apps.user.access.scope_to_organization``). El admin de Django
no pasa por DRF y no cambia: es donde se corrige un usuario mal configurado.
"""
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

from apps.user.access import scope_to_organization


class _OrganizationScopeMixin:
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, auth = result
        return scope_to_organization(user), auth


class OrgScopedJWTAuthentication(_OrganizationScopeMixin, JWTAuthentication):
    pass


class OrgScopedSessionAuthentication(_OrganizationScopeMixin, SessionAuthentication):
    pass


class OrgScopedBasicAuthentication(_OrganizationScopeMixin, BasicAuthentication):
    pass
