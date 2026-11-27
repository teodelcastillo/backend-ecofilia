"""
Las etapas dejan de lanzar ``caf-enverdecimiento-operacion`` (27/11/2026).

El agente se eliminó, pero su slug quedó en ``stages[].launches`` de la
configuración de CAF: las configuraciones nombran skills por slug, sin clave
foránea. Cada cambio de etapa lo seguía intentando lanzar —quedaba como
``unavailable`` en la transición— y la consola ya no podía guardar la
configuración de CAF, porque la validación rechaza una skill que no existe.

Lo saca de toda configuración guardada: instituciones y configuraciones de
plataforma (la plantilla de las instituciones nuevas incluida). Desde ahora,
eliminar una skill la saca sola (``institution_config.quitar_skill_de_configuraciones``).

No se revierte: volver a ponerlo dejaría la configuración apuntando a un
agente que no existe.
"""
import copy

from django.db import migrations

SLUG = "caf-enverdecimiento-operacion"


def _sin_el_agente(config) -> dict | None:
    """La configuración sin el agente en ``launches``, o None si no lo tenía."""
    if not isinstance(config, dict):
        return None
    config = copy.deepcopy(config)
    cambio = False
    for stage in config.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        launches = stage.get("launches") or []
        if SLUG in launches:
            stage["launches"] = [s for s in launches if s != SLUG]
            cambio = True
    return config if cambio else None


def aplicar(apps, schema_editor):
    Organization = apps.get_model("user", "Organization")
    PlatformConfig = apps.get_model("user", "PlatformConfig")
    for model in (Organization, PlatformConfig):
        for fila in model.objects.all():
            config = _sin_el_agente(fila.config)
            if config is not None:
                fila.config = config
                fila.save(update_fields=["config"])


class Migration(migrations.Migration):
    dependencies = [("user", "0019_caf_dec_foto_independiente")]

    operations = [migrations.RunPython(aplicar, migrations.RunPython.noop)]
