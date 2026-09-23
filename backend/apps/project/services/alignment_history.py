"""
Historial de la determinación de alineación con París.

Cada vez que una edición de la operación cambia su determinación o su etapa,
se guarda una foto (``ProjectAlignmentSnapshot``). Es lo que permite al
tablero mostrar la evolución mes a mes y comparar cómo estaba cada operación en
IDO con cómo quedó en DEC, con datos reales en vez de ejemplos.
"""
from __future__ import annotations

from django.utils import timezone

from apps.project.models import Project, ProjectAlignmentSnapshot

ALINEACION_KEY = "alineacion_paris"
# Metadatos de la edición: cambian en cada guardado aunque la determinación
# sea la misma, así que no cuentan como cambio.
_METADATA_KEYS = {"actualizado_en", "actualizado_por"}


def _notes(value) -> dict:
    return value if isinstance(value, dict) else {}


def _determinacion(notes: dict) -> dict:
    raw = notes.get(ALINEACION_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        k: v.strip()
        for k, v in raw.items()
        if k not in _METADATA_KEYS and isinstance(v, str) and v.strip()
    }


def _estado(notes: dict) -> str:
    value = notes.get("estado")
    return value.strip() if isinstance(value, str) else ""


def record_if_changed(project: Project, previous_notes, user=None) -> ProjectAlignmentSnapshot | None:
    """Guarda una foto si la determinación o la etapa cambiaron.

    Una operación sin determinación no genera fotos: cambiarle la etapa antes
    de determinar nada no es historia de alineación.
    """
    before = _notes(previous_notes)
    after = _notes(project.context_notes)
    det_after = _determinacion(after)
    if not det_after:
        return None
    if det_after == _determinacion(before) and _estado(after) == _estado(before):
        return None
    monto = after.get("monto")
    return ProjectAlignmentSnapshot.objects.create(
        project=project,
        estado=_estado(after),
        alineacion=det_after,
        monto=str(monto).strip() if monto is not None else "",
        captured_at=timezone.now(),
        captured_by=user if getattr(user, "is_authenticated", False) else None,
    )
