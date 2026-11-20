"""
CAF: el informe de DEC hereda el contexto de clima y biodiversidad del de IDO.

Si la operación pasó por IDO en Ecofilia, la corrida de DEC arranca con los
pasos «B.» ya escritos, copiados del informe de IDO, y no los vuelve a generar
(``apps.project.services.stage_inheritance``). Sin IDO, el DEC corre completo.

También lee el IET de IDO emitido: al lanzar el DEC se vincula a la operación
con la etiqueta ``iet-datbc-ido`` (``document/0028``), que se suma al grupo de
documentos de la operación para quien trae un IET IDO hecho por fuera.

Si no existe la institución ``caf``, no tiene configuración o no tiene etapa
``dec``, no hace nada. Reversible.
"""
import copy

from django.db import migrations

CAF_SLUG = "caf"
DEC_STAGE = "dec"
HEREDA = {"de": "ido_cci", "prefijo": "B.", "etiqueta": "iet-datbc-ido"}
GROUP = "operacion"


def _con(apps, fn):
    Organization = apps.get_model("user", "Organization")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None or not caf.config:
        return
    config = copy.deepcopy(caf.config)
    stages = config.get("stages") or []
    if not any(s.get("slug") == HEREDA["de"] for s in stages):
        return
    for stage in stages:
        if stage.get("slug") == DEC_STAGE:
            fn(stage)
    for group in config.get("evidence_tag_groups") or []:
        if group.get("slug") == GROUP:
            tags = [t for t in group.get("tags") or [] if t != HEREDA["etiqueta"]]
            if fn is not _sacar:
                at = tags.index("otros") if "otros" in tags else len(tags)
                tags.insert(at, HEREDA["etiqueta"])
            group["tags"] = tags
    caf.config = config
    caf.save(update_fields=["config"])


def _poner(stage):
    stage["hereda"] = dict(HEREDA)


def _sacar(stage):
    stage.pop("hereda", None)


def aplicar(apps, schema_editor):
    _con(apps, _poner)


def revertir(apps, schema_editor):
    _con(apps, _sacar)


class Migration(migrations.Migration):
    dependencies = [
        ("user", "0016_caf_dec_y_mision"),
        ("document", "0028_iet_ido_caf"),
    ]

    operations = [migrations.RunPython(aplicar, revertir)]
