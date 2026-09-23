"""
Primera foto de la determinación de cada operación que ya la tiene cargada.

Hasta acá la determinación se sobrescribía sin dejar historia. Esta foto es el
punto de partida: la fecha es la de la última edición de la determinación
(``actualizado_en``, que la pantalla de alineación guarda) o, si no está, la
última modificación de la operación. Queda marcada como ``backfill`` para que
el tablero no la confunda con una edición registrada en su momento.
"""
from datetime import datetime

from django.db import migrations
from django.utils import timezone

_METADATA_KEYS = {"actualizado_en", "actualizado_por"}


def _fecha(raw, fallback):
    if isinstance(raw, str) and raw.strip():
        try:
            value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return value if timezone.is_aware(value) else timezone.make_aware(value)
        except ValueError:
            pass
    return fallback


def forwards(apps, schema_editor):
    Project = apps.get_model("project", "Project")
    Snapshot = apps.get_model("project", "ProjectAlignmentSnapshot")
    for project in Project.objects.all().only("id", "context_notes", "updated_at").iterator():
        notes = project.context_notes if isinstance(project.context_notes, dict) else {}
        raw = notes.get("alineacion_paris")
        if not isinstance(raw, dict):
            continue
        det = {
            k: v.strip()
            for k, v in raw.items()
            if k not in _METADATA_KEYS and isinstance(v, str) and v.strip()
        }
        if not det:
            continue
        estado = notes.get("estado")
        monto = notes.get("monto")
        Snapshot.objects.create(
            project_id=project.id,
            estado=estado.strip() if isinstance(estado, str) else "",
            alineacion=det,
            monto=str(monto).strip() if monto is not None else "",
            captured_at=_fecha(raw.get("actualizado_en"), project.updated_at),
            source="backfill",
        )


def backwards(apps, schema_editor):
    apps.get_model("project", "ProjectAlignmentSnapshot").objects.filter(source="backfill").delete()


class Migration(migrations.Migration):
    dependencies = [("project", "0013_alignment_snapshots")]
    operations = [migrations.RunPython(forwards, backwards)]
