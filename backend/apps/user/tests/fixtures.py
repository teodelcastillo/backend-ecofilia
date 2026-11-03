"""Datos de prueba compartidos entre apps.

``caf_config`` es la configuración de CAF tal como la siembra la migración
``user/0009_config_caf``: un test que necesita «una operación de CAF» la
necesita entera, porque desde la fase 1 las etapas, los lanzamientos y los
criterios salen de ahí.
"""
import copy
import importlib


def caf_config() -> dict:
    return copy.deepcopy(importlib.import_module("apps.user.migrations.0009_config_caf").CAF_CONFIG)


def make_superadmin(email: str = "root@ecofilia.site", **extra):
    """Un superadmin de Ecofilia con MFA activo, como lo exigen las acciones de
    plataforma (``ADMIN_REQUIRE_MFA``)."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(
        email=email, password="x", mfa_enabled=True, mfa_secret="JBSWY3DPEHPK3PXP", **extra
    )
