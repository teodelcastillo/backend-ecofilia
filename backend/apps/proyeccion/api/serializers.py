"""Cómo sale cada cosa por la API. Son funciones: el módulo no tiene formularios."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.proyeccion.analisis import es_proxima, sin_informacion, tiene_alerta
from apps.proyeccion.lectura import en_pipeline
from apps.proyeccion.models import EstadoAnalisis, FichaNDC, Nivel, OperacionProyectada, Proyeccion

# Sin latido en este tiempo, un análisis «en curso» se da por caído y se puede relanzar.
MINUTOS_TRABADO = 10


def esta_trabada(proyeccion: Proyeccion) -> bool:
    if proyeccion.estado not in (EstadoAnalisis.EN_COLA, EstadoAnalisis.ANALIZANDO):
        return False
    referencia = proyeccion.latido or proyeccion.actualizado_en
    return referencia < timezone.now() - timedelta(minutes=MINUTOS_TRABADO)


def prioridad(op: OperacionProyectada) -> str:
    """ALTA con algún ROJO en las alertas de riesgo o brecha; MEDIA con algún AMARILLO."""
    niveles = (op.no_alineadas, op.riesgo_transicion, op.brecha_ndc)
    if Nivel.ROJO in niveles:
        return "ALTA"
    if Nivel.AMARILLO in niveles:
        return "MEDIA"
    return ""


def operacion(op: OperacionProyectada) -> dict:
    return {
        "id": op.pk,
        "codigo": op.codigo,
        "status": op.status,
        "pais": op.pais,
        "cliente": op.cliente,
        "descripcion": op.descripcion,
        "producto": op.producto,
        "monto_usd_mm": float(op.monto_usd_mm) if op.monto_usd_mm is not None else None,
        "fecha_cci_ido": op.fecha_cci_ido.isoformat() if op.fecha_cci_ido else None,
        "fecha_cci_ido_texto": op.fecha_cci_ido_texto,
        "proxima": es_proxima(op.fecha_cci_ido),
        "en_pipeline": en_pipeline(op.status),
        "nueva": op.nueva,
        "posible_duplicado": op.posible_duplicado,
        "analizada": op.analizada,
        "tipo_operacion": op.tipo_operacion,
        "no_alineadas": op.no_alineadas,
        "nota_no_alineadas": op.nota_no_alineadas,
        "riesgo_transicion": op.riesgo_transicion,
        "riesgo_categoria": op.riesgo_categoria,
        "nota_riesgo": op.nota_riesgo,
        "oportunidad_ndc": op.oportunidad_ndc,
        "nota_oportunidad": op.nota_oportunidad,
        "brecha_ndc": op.brecha_ndc,
        "nota_brecha": op.nota_brecha,
        "fuente_base": op.fuente_base,
        "fuente_ndc": op.fuente_ndc,
        "prioridad": prioridad(op),
        "tiene_alerta": tiene_alerta(op),
    }


def proyeccion_resumida(proyeccion: Proyeccion, operaciones: list[OperacionProyectada] | None = None) -> dict:
    if operaciones is None:
        operaciones = list(proyeccion.operaciones.all())
    pipeline = [op for op in operaciones if en_pipeline(op.status)]
    analizadas = [op for op in operaciones if op.analizada]
    return {
        "id": proyeccion.pk,
        "periodo": proyeccion.periodo.strftime("%Y-%m"),
        "archivo_nombre": proyeccion.archivo_nombre,
        "hoja": proyeccion.hoja,
        "creado_en": proyeccion.creado_en.isoformat(),
        "creado_por": getattr(proyeccion.creado_por, "email", None),
        "estado": proyeccion.estado,
        "trabada": esta_trabada(proyeccion),
        "etapa": proyeccion.etapa,
        "progreso_hechas": proyeccion.progreso_hechas,
        "progreso_total": proyeccion.progreso_total,
        "analizado_en": proyeccion.analizado_en.isoformat() if proyeccion.analizado_en else None,
        "alcance": proyeccion.alcance or {},
        "aviso": proyeccion.aviso,
        "error": proyeccion.error,
        "total_operaciones": len(operaciones),
        "en_pipeline": len(pipeline),
        "analizadas": len(analizadas),
        "con_alertas": sum(1 for op in analizadas if tiene_alerta(op)),
        "prioridad_alta": sum(1 for op in analizadas if prioridad(op) == "ALTA"),
        "sin_informacion": sum(1 for op in analizadas if sin_informacion(op)),
    }


def proyeccion_completa(proyeccion: Proyeccion, salieron: list[OperacionProyectada]) -> dict:
    operaciones = list(proyeccion.operaciones.all())
    estados: dict[str, int] = {}
    for op in operaciones:
        estados[op.status] = estados.get(op.status, 0) + 1
    return {
        **proyeccion_resumida(proyeccion, operaciones),
        "resumen": proyeccion.resumen,
        "modelo": proyeccion.modelo,
        "estados": [
            {"status": status, "cantidad": cantidad, "en_pipeline": en_pipeline(status)}
            for status, cantidad in sorted(estados.items(), key=lambda par: (-par[1], par[0]))
        ],
        "salieron": [
            {"pais": op.pais, "cliente": op.cliente, "descripcion": op.descripcion} for op in salieron[:100]
        ],
        "operaciones": [operacion(op) for op in operaciones],
    }


def ficha(f: FichaNDC) -> dict:
    return {
        "id": f.pk,
        "pais": f.pais,
        "estado": f.estado,
        "version_ndc": f.version_ndc,
        "contenido": f.contenido,
        # La NDC de la biblioteca con la que se armó.
        "documento_slug": f.documento.slug if f.documento_id and f.documento else None,
        "documento_nombre": f.documento_nombre,
        "documento_anio": f.documento_anio,
        "error": f.error,
        "actualizado_en": f.actualizado_en.isoformat(),
    }


def ndc_de_biblioteca(documento) -> dict:
    return {
        "pais": documento.region,
        "documento_id": documento.pk,
        "documento_slug": documento.slug,
        "documento_nombre": documento.name,
        "documento_anio": documento.year,
    }
