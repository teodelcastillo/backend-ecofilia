"""
Historial de la mirada del ejecutivo: alineación con París y enverdecimiento.

Cada vez que una edición de la operación cambia su determinación, su
enverdecimiento o su etapa, se guarda una foto (``ProjectAlignmentSnapshot``).
Es lo que permite al tablero mostrar la evolución mes a mes, comparar cómo
estaba cada operación en IDO con cómo quedó en DEC, y al Resumen de una
operación mostrarla en un momento anterior.
"""
from __future__ import annotations

from django.utils import timezone

from apps.project.models import Project, ProjectAlignmentSnapshot
from apps.project.services.caf_criteria import executive_enverdecimiento

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


def record_if_changed(
    project: Project,
    previous_notes,
    user=None,
    *,
    source: str = ProjectAlignmentSnapshot.Source.EDIT,
) -> ProjectAlignmentSnapshot | None:
    """Guarda una foto si la determinación, el enverdecimiento o la etapa cambiaron.

    Una operación sin determinación ni enverdecimiento no genera fotos:
    cambiarle la etapa antes de mirar nada no es historia. ``source`` dice qué
    la originó: la del cambio de etapa (``transition``) lleva la determinación
    de la etapa que se dejó, y no cuenta como resultado de la nueva.
    """
    before = _notes(previous_notes)
    after = _notes(project.context_notes)
    det_after = _determinacion(after)
    green_after = executive_enverdecimiento(after)
    if not det_after and not green_after:
        return None
    if (
        det_after == _determinacion(before)
        and green_after == executive_enverdecimiento(before)
        and _estado(after) == _estado(before)
    ):
        return None
    monto = after.get("monto")
    return ProjectAlignmentSnapshot.objects.create(
        project=project,
        estado=_estado(after),
        alineacion=det_after,
        enverdecimiento=green_after,
        monto=str(monto).strip() if monto is not None else "",
        captured_at=timezone.now(),
        captured_by=user if getattr(user, "is_authenticated", False) else None,
        source=source,
    )


def record_emission(report, user=None) -> ProjectAlignmentSnapshot | None:
    """La foto de la mirada oficial de una etapa: la que quedó en su informe emitido.

    Se guarda siempre, aunque no haya cambiado nada desde la última edición:
    es la que el tablero compara entre etapas.
    """
    assessment = report.emitted_assessment or {}
    determinaciones = {
        k: v for k, v in (assessment.get("determinaciones") or {}).items() if isinstance(v, str) and v.strip()
    }
    enverdecimiento = assessment.get("enverdecimiento") or {}
    if not determinaciones and not enverdecimiento:
        return None
    monto = (report.project.context_notes or {}).get("monto")
    return ProjectAlignmentSnapshot.objects.create(
        project=report.project,
        estado=report.stage,
        alineacion=determinaciones,
        enverdecimiento=enverdecimiento,
        monto=str(monto).strip() if monto is not None else "",
        captured_at=report.emitted_at or timezone.now(),
        captured_by=user if getattr(user, "is_authenticated", False) else None,
        source=ProjectAlignmentSnapshot.Source.EMISSION,
        report=report,
    )
