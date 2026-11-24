"""
Lo que el informe de una etapa reusa del de la etapa anterior.

En CAF, si la operación hizo su IDO en Ecofilia, el DEC no vuelve a analizar
el contexto de clima y biodiversidad: respeta el del IDO. La etapa lo declara
en su configuración (``stages[].hereda = {"de": "ido_cci", "prefijo": "B."}``)
y, al lanzar su informe, la corrida arranca con esos pasos ya escritos.

Funciona porque el motor retoma una corrida desde el primer paso que no tiene
escrito (``apps.skill.services._run_copilot``): los pasos heredados tienen que
ser los primeros del workflow, en el mismo orden y con los mismos títulos que
en el informe de origen. Si no lo son, no se hereda nada y la corrida corre
completa: es preferible repetir el contexto a mezclar secciones que no se
corresponden.

El texto heredado es el vigente del informe de origen: lo publicado por el
ejecutivo si editó la sección, si no lo que escribió el agente.
"""
from __future__ import annotations

import copy
import logging

from apps.project.models import Project
from apps.user import institution_config

logger = logging.getLogger(__name__)

HEREDADO_KEY = "heredado_de"


def _titulo(value) -> str:
    return " ".join(str(value or "").split())


def herencia(project: Project, stage: str) -> dict | None:
    """La regla de herencia de la etapa, o None."""
    found = institution_config.stage(institution_config.config_for_project(project), stage)
    hereda = (found or {}).get("hereda")
    return hereda if isinstance(hereda, dict) and hereda.get("de") and hereda.get("prefijo") else None


def pasos_heredados(project: Project, stage: str, skill) -> list[dict] | None:
    """Los pasos ya escritos con los que arranca el informe de ``stage``.

    None si la etapa no hereda, si la operación no tiene un informe de
    Ecofilia terminado en la etapa de origen o si los pasos no se corresponden
    (ver el docstring del módulo).
    """
    from apps.project.services.stage_reports import current_ecofilia_report
    from apps.skill.models import ExecutionSectionEdit

    regla = herencia(project, stage)
    if regla is None:
        return None
    # «Hizo el IDO en Ecofilia» es que corrió su informe: una operación creada
    # directamente en DEC (o con un IDO hecho por fuera) analiza el contexto.
    origen = current_ecofilia_report(project, regla["de"])
    if origen is None:
        return None

    prefijo = regla["prefijo"]
    destino = [s for s in skill.steps.order_by("position")]
    heredables = []
    for step in destino:
        if not _titulo(step.title).startswith(prefijo):
            break
        heredables.append(step)
    if not heredables:
        return None
    if any(_titulo(s.title).startswith(prefijo) for s in destino[len(heredables):]):
        logger.warning(
            "Operación %s: los pasos «%s» del informe de %s no están todos al principio; "
            "no se heredan.", project.pk, prefijo, stage,
        )
        return None

    escritos = {
        _titulo(entry.get("title")): entry
        for entry in (origen.output_structured or {}).get("steps") or []
        if isinstance(entry, dict) and _titulo(entry.get("title")).startswith(prefijo)
    }
    faltan = [s.title for s in heredables if _titulo(s.title) not in escritos]
    if faltan:
        logger.warning(
            "Operación %s: el informe de %s (corrida %s) no tiene %s; el de %s corre completo.",
            project.pk, regla["de"], origen.pk, ", ".join(faltan), stage,
        )
        return None

    publicados = {
        edit.step_id: edit.published
        for edit in ExecutionSectionEdit.objects.filter(execution=origen).exclude(published="")
    }
    pasos = []
    for step in heredables:
        entry = copy.deepcopy(escritos[_titulo(step.title)])
        texto = publicados.get(entry.get("step_id"))
        if texto:
            entry["content"] = texto
        entry["step_id"] = step.id
        entry["title"] = step.title
        entry[HEREDADO_KEY] = {"execution_id": origen.pk, "stage": regla["de"]}
        pasos.append(entry)
    return pasos


FUENTE_PREFIX = "informe-etapa:"


def _informe_emitido(project: Project, stage: str):
    from apps.project.models import ProjectStageReport

    return (
        ProjectStageReport.objects.filter(
            project=project,
            stage=stage,
            voided_at__isnull=True,
            superseded_at__isnull=True,
            emitted_at__isnull=False,
        )
        .select_related("document", "created_by")
        .order_by("-emitted_at", "-id")
        .first()
    )


def vincular_informe_anterior(project: Project, stage: str, user=None):
    """Vincula a la operación el informe emitido de la etapa de origen.

    Así el análisis de ``stage`` lo lee como evidencia (en CAF, el DEC lee el
    IET IDO con la etiqueta ``iet-datbc-ido``). Es una excepción deliberada a
    que las corridas no lean la conclusión del ejecutivo: vale sólo para la
    etapa anterior, cuyo informe ya está emitido y no cambia.

    Si el informe se subió (hecho por fuera), se vincula ese documento; si se
    armó en Ecofilia, se crea un documento de texto con lo emitido. Es
    idempotente por informe: una versión nueva reemplaza el vínculo de la
    anterior. Devuelve el documento vinculado, o None si no corresponde.
    """
    from django.core.files.base import ContentFile

    from apps.document.models import Document
    from apps.project.models import ProjectDocument
    from apps.project.services.evidence_tags import project_evidence_tags

    regla = herencia(project, stage)
    etiqueta = (regla or {}).get("etiqueta")
    if not etiqueta:
        return None
    report = _informe_emitido(project, regla["de"])
    if report is None:
        return None
    tag = project_evidence_tags(project.pk).filter(slug=etiqueta).first()
    if tag is None:
        logger.warning("Operación %s: no existe la etiqueta «%s».", project.pk, etiqueta)
        return None

    marca = f"{FUENTE_PREFIX}{report.pk}"
    # Vínculos de versiones anteriores del mismo informe: se reemplazan.
    ProjectDocument.objects.filter(
        project=project, document__source__startswith=FUENTE_PREFIX, document__evidence_tags=tag
    ).exclude(document__source=marca).delete()

    if report.document_id and not report.emitted_markdown.strip():
        documento = report.document
    else:
        documento = (
            Document.objects.filter(source=marca, owner=report.created_by or project.owner).first()
        )
        if documento is None:
            texto = (report.emitted_markdown or "").strip()
            if not texto:
                return None
            etapa = (regla["de"] or "").replace("_cci", "").upper()
            documento = Document.objects.create(
                owner=report.created_by or project.owner,
                name=f"IET {etapa} emitido — {project.name}"[:255],
                description=f"Informe del ejecutivo de la etapa {etapa}, emitido el "
                f"{report.emitted_at:%d/%m/%Y}. Vinculado automáticamente al pasar de etapa.",
                source=marca,
                file=ContentFile(texto.encode("utf-8"), name=f"iet-{etapa.lower()}-{project.slug}.txt"),
            )
    documento.evidence_tags.add(tag)
    ProjectDocument.objects.get_or_create(
        project=project, document=documento, defaults={"added_by": user if getattr(user, "is_authenticated", False) else None}
    )
    return documento


def informes_de_etapas_anteriores(project: Project) -> set[int]:
    """Los documentos de la operación que son el informe de una etapa anterior.

    Son los que vinculó ``vincular_informe_anterior`` y los que alguien subió
    con la etiqueta de herencia de alguna etapa (en CAF, un IET IDO hecho por
    fuera, cargado con ``iet-datbc-ido``). El análisis los recibe aparte y sin
    citas: no son evidencia de la etapa (ver
    ``apps.skill.context_budget.DocumentDelivery.is_reference``).
    """
    from django.db.models import Q

    from apps.project.models import ProjectDocument

    config = institution_config.config_for_project(project)
    etiquetas = {
        s["hereda"]["etiqueta"]
        for s in config.get("stages") or []
        if isinstance(s.get("hereda"), dict) and s["hereda"].get("etiqueta")
    }
    criterio = Q(document__source__startswith=FUENTE_PREFIX)
    if etiquetas:
        criterio |= Q(document__evidence_tags__slug__in=etiquetas)
    return set(
        ProjectDocument.objects.filter(project=project)
        .filter(criterio)
        .values_list("document_id", flat=True)
    )
