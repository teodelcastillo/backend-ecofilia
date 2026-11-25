"""
CAF: el DEC es una foto independiente de la operación (25/11/2026).

El informe de DEC pasa a ser un duplicado del asistente de IDO, que es el que
funciona bien («DEC — IET DATBC — Informe de Evaluación Técnica de la
Operación»). Corre sobre los documentos del DEC, con su propia determinación del
ejecutivo, y sólo hereda del IDO el contexto de clima y biodiversidad (los pasos
«B.»). Ya no lee el IET IDO: la regla de herencia queda sin etiqueta.

Si no existe la institución ``caf``, no tiene configuración o no tiene etapa
``dec``, no hace nada. Reversible: vuelve al asistente de DEC anterior.
"""
import copy

from django.db import migrations

CAF_SLUG = "caf"
DEC_STAGE = "dec"
ASISTENTE = "ido-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion-copia"
ANTERIOR = "dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion"
HEREDA = {"de": "ido_cci", "prefijo": "B."}
HEREDA_ANTERIOR = {"de": "ido_cci", "prefijo": "B.", "etiqueta": "iet-datbc-ido"}


def _con(apps, fn):
    Organization = apps.get_model("user", "Organization")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None or not caf.config:
        return
    config = copy.deepcopy(caf.config)
    cambio = False
    for stage in config.get("stages") or []:
        if stage.get("slug") == DEC_STAGE:
            fn(stage)
            cambio = True
    if cambio:
        caf.config = config
        caf.save(update_fields=["config"])


def _usar(asistente, hereda):
    def fn(stage):
        stage["report"] = asistente
        launches = [s for s in stage.get("launches") or [] if s not in (ASISTENTE, ANTERIOR)]
        stage["launches"] = [asistente, *launches]
        stage["hereda"] = dict(hereda)

    return fn


def aplicar(apps, schema_editor):
    _con(apps, _usar(ASISTENTE, HEREDA))


def revertir(apps, schema_editor):
    _con(apps, _usar(ANTERIOR, HEREDA_ANTERIOR))


class Migration(migrations.Migration):
    dependencies = [("user", "0017_caf_dec_hereda_contexto")]

    operations = [migrations.RunPython(aplicar, revertir)]
