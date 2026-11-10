"""Guardar la proyección de un mes y compararla con la del mes anterior."""
from __future__ import annotations

from collections import Counter
from datetime import date

from django.db import transaction

from apps.proyeccion.lectura import LecturaProyeccion, clave_operacion, en_pipeline
from apps.proyeccion.models import EstadoAnalisis, OperacionProyectada, Proyeccion


def _clave(op: OperacionProyectada) -> str:
    return clave_operacion(op.pais, op.cliente, op.descripcion)


def anterior_a(proyeccion: Proyeccion) -> Proyeccion | None:
    return (
        Proyeccion.objects.filter(organization=proyeccion.organization, periodo__lt=proyeccion.periodo)
        .order_by("-periodo")
        .first()
    )


def marcar_nuevas(proyeccion: Proyeccion) -> None:
    """Qué operaciones no estaban en la proyección anterior (None si no hay anterior)."""
    anterior = anterior_a(proyeccion)
    operaciones = list(proyeccion.operaciones.all())
    if anterior is None:
        for op in operaciones:
            op.nueva = None
    else:
        previas = {_clave(op) for op in anterior.operaciones.all()}
        for op in operaciones:
            op.nueva = _clave(op) not in previas
    OperacionProyectada.objects.bulk_update(operaciones, ["nueva"], batch_size=500)


def salieron_del_pipeline(proyeccion: Proyeccion) -> list[OperacionProyectada]:
    """Operaciones que el mes anterior estaban en el pipeline y este mes no figuran."""
    anterior = anterior_a(proyeccion)
    if anterior is None:
        return []
    actuales = {_clave(op) for op in proyeccion.operaciones.all()}
    vistas: set[str] = set()
    salieron = []
    for op in anterior.operaciones.all():
        clave = _clave(op)
        if en_pipeline(op.status) and clave not in actuales and clave not in vistas:
            vistas.add(clave)
            salieron.append(op)
    return salieron


@transaction.atomic
def guardar_proyeccion(
    *, organization, periodo: date, lectura: LecturaProyeccion, archivo_nombre: str, usuario
) -> Proyeccion:
    """Crea la proyección del mes, o la reemplaza entera si ya existía."""
    proyeccion, creada = Proyeccion.objects.get_or_create(
        organization=organization,
        periodo=periodo,
        defaults={"creado_por": usuario},
    )
    if not creada:
        proyeccion.operaciones.all().delete()
    proyeccion.archivo_nombre = archivo_nombre[:255]
    proyeccion.hoja = lectura.hoja[:255]
    proyeccion.creado_por = usuario
    proyeccion.estado = EstadoAnalisis.SIN_ANALIZAR
    proyeccion.alcance = {}
    proyeccion.etapa = ""
    proyeccion.progreso_hechas = 0
    proyeccion.progreso_total = 0
    proyeccion.latido = None
    proyeccion.analizado_en = None
    proyeccion.resumen = ""
    proyeccion.aviso = ""
    proyeccion.error = ""
    proyeccion.modelo = ""
    proyeccion.uso = {}
    proyeccion.save()

    repeticiones = Counter(
        clave_operacion(f.pais, f.cliente, f.descripcion) for f in lectura.filas if en_pipeline(f.status)
    )
    OperacionProyectada.objects.bulk_create(
        [
            OperacionProyectada(
                proyeccion=proyeccion,
                orden=indice,
                codigo=f"OP-{indice:03d}",
                status=fila.status,
                pais=fila.pais,
                cliente=fila.cliente,
                descripcion=fila.descripcion,
                producto=fila.producto,
                monto_usd_mm=fila.monto_usd_mm,
                fecha_cci_ido_texto=fila.fecha_cci_ido_texto,
                fecha_cci_ido=fila.fecha_cci_ido,
                posible_duplicado=en_pipeline(fila.status)
                and repeticiones[clave_operacion(fila.pais, fila.cliente, fila.descripcion)] > 1,
            )
            for indice, fila in enumerate(lectura.filas, start=1)
        ],
        batch_size=500,
    )
    marcar_nuevas(proyeccion)
    # Si se cargó un mes viejo, el siguiente se compara ahora contra este.
    siguiente = (
        Proyeccion.objects.filter(organization=organization, periodo__gt=periodo).order_by("periodo").first()
    )
    if siguiente is not None:
        marcar_nuevas(siguiente)
    return proyeccion
