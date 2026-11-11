"""API del módulo de proyección (``/api/proyeccion/``).

Todo lo que devuelve es de una sola institución: la del miembro que pregunta,
o la que el superadmin nombra con ``?organization=``. Un id de otra
institución responde 404, igual que uno que no existe.
"""
from __future__ import annotations

import re
from datetime import date

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.proyeccion import biblioteca, servicios, tasks
from apps.proyeccion.acceso import organizacion_del_pedido, puede_entrar
from apps.proyeccion.analisis import campos_de_documento, pais_con_ndc
from apps.proyeccion.api import serializers
from apps.proyeccion.lectura import MAX_BYTES, ArchivoInvalido, leer_proyeccion, plegar
from apps.proyeccion.models import (
    AlcanceNDC,
    EstadoAnalisis,
    EstadoFicha,
    FichaNDC,
    Proyeccion,
)



def _periodo(valor) -> date:
    coincidencia = re.fullmatch(r"(\d{4})-(\d{1,2})", str(valor or "").strip())
    if not coincidencia or not 1 <= int(coincidencia.group(2)) <= 12:
        raise ValidationError({"periodo": ["Indicá el mes como AAAA-MM."]})
    return date(int(coincidencia.group(1)), int(coincidencia.group(2)), 1)


def _proyeccion(request, pk: int) -> Proyeccion:
    proyeccion = Proyeccion.objects.select_related("organization", "creado_por").filter(pk=pk).first()
    if proyeccion is None or not puede_entrar(request.user, proyeccion.organization):
        raise NotFound("No existe esa proyección.")
    return proyeccion


def _ficha(request, pk: int) -> FichaNDC:
    ficha = FichaNDC.objects.select_related("organization").filter(pk=pk).first()
    if ficha is None or not puede_entrar(request.user, ficha.organization):
        raise NotFound("No existe esa ficha.")
    return ficha


class ProyeccionesView(APIView):
    """Los meses cargados, y cargar el de un mes."""

    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def get(self, request):
        organization = organizacion_del_pedido(request)
        proyecciones = (
            Proyeccion.objects.filter(organization=organization)
            .select_related("creado_por")
            .prefetch_related("operaciones")
        )
        return Response([serializers.proyeccion_resumida(p) for p in proyecciones])

    def post(self, request):
        organization = organizacion_del_pedido(request)
        periodo = _periodo(request.data.get("periodo"))
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": ["Adjuntá el Excel o CSV de la proyección."]})
        if archivo.size > MAX_BYTES:
            raise ValidationError({"file": ["El archivo pesa más de 15 MB."]})

        existente = Proyeccion.objects.filter(organization=organization, periodo=periodo).first()
        reemplazar = str(request.data.get("reemplazar", "")).lower() in ("1", "true", "si", "sí")
        if existente is not None and not reemplazar:
            return Response(
                {
                    "detail": "Ya hay una proyección cargada para ese mes.",
                    "code": "ya_existe",
                    "id": existente.pk,
                },
                status=status.HTTP_409_CONFLICT,
            )
        if existente is not None and existente.estado in (EstadoAnalisis.EN_COLA, EstadoAnalisis.ANALIZANDO):
            if not serializers.esta_trabada(existente):
                raise ValidationError({"detail": "Esa proyección se está analizando: esperá a que termine."})

        try:
            lectura = leer_proyeccion(archivo.name, archivo.read())
        except ArchivoInvalido as exc:
            raise ValidationError({"file": [str(exc)]}) from exc

        proyeccion = servicios.guardar_proyeccion(
            organization=organization,
            periodo=periodo,
            lectura=lectura,
            archivo_nombre=archivo.name,
            usuario=request.user,
        )
        return Response(
            serializers.proyeccion_completa(proyeccion, servicios.salieron_del_pipeline(proyeccion)),
            status=status.HTTP_201_CREATED,
        )


class ProyeccionView(APIView):
    def get(self, request, pk: int):
        proyeccion = _proyeccion(request, pk)
        return Response(serializers.proyeccion_completa(proyeccion, servicios.salieron_del_pipeline(proyeccion)))

    def delete(self, request, pk: int):
        proyeccion = _proyeccion(request, pk)
        organization, periodo = proyeccion.organization, proyeccion.periodo
        proyeccion.delete()
        siguiente = (
            Proyeccion.objects.filter(organization=organization, periodo__gt=periodo).order_by("periodo").first()
        )
        if siguiente is not None:
            servicios.marcar_nuevas(siguiente)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AnalizarView(APIView):
    """Lanza el screening. Responde enseguida: el avance se consulta en el detalle."""

    def post(self, request, pk: int):
        proyeccion = _proyeccion(request, pk)
        estados = request.data.get("statuses")
        if estados is not None and (
            not isinstance(estados, list) or not all(isinstance(e, str) for e in estados)
        ):
            raise ValidationError({"statuses": ["Tiene que ser una lista de estados del Excel."]})
        ndc = request.data.get("ndc") or AlcanceNDC.TODOS
        if ndc not in AlcanceNDC.values:
            raise ValidationError({"ndc": [f"Tiene que ser uno de: {', '.join(AlcanceNDC.values)}."]})

        with transaction.atomic():
            actual = Proyeccion.objects.select_for_update().get(pk=proyeccion.pk)
            en_curso = actual.estado in (EstadoAnalisis.EN_COLA, EstadoAnalisis.ANALIZANDO)
            if en_curso and not serializers.esta_trabada(actual):
                raise ValidationError({"detail": "El análisis ya está en curso."})
            actual.estado = EstadoAnalisis.EN_COLA
            actual.alcance = {"statuses": estados or [], "ndc": ndc}
            actual.etapa = "en_cola"
            actual.progreso_hechas = 0
            actual.progreso_total = 0
            actual.latido = timezone.now()
            actual.error = ""
            actual.aviso = ""
            actual.save()

        try:
            tasks.despachar_analisis(proyeccion.pk)
        except Exception as exc:  # noqa: BLE001 — la cola no respondió
            Proyeccion.objects.filter(pk=proyeccion.pk).update(
                estado=EstadoAnalisis.ERROR,
                etapa="",
                error=f"No se pudo poner el análisis en la cola de trabajo ({exc}). Volvé a lanzarlo.",
            )
        proyeccion.refresh_from_db()
        return Response(serializers.proyeccion_resumida(proyeccion), status=status.HTTP_202_ACCEPTED)


class FichasView(APIView):
    """Las fichas NDC de la institución y qué NDC hay en la biblioteca pública."""

    def get(self, request):
        organization = organizacion_del_pedido(request)
        fichas = FichaNDC.objects.filter(organization=organization).select_related("documento")
        return Response(
            {
                "fichas": [serializers.ficha(f) for f in fichas],
                # Qué países tienen NDC en la biblioteca, para mostrar de dónde
                # sale (o saldría) cada ficha.
                "biblioteca": [serializers.ndc_de_biblioteca(d) for d in biblioteca.ndc_vigentes().values()],
            }
        )

    def post(self, request):
        """Arma (o vuelve a armar) la ficha de un país con su NDC vigente de la biblioteca."""
        organization = organizacion_del_pedido(request)
        pais = re.sub(r"\s+", " ", str(request.data.get("pais") or "")).strip()[:120]
        if not pais:
            raise ValidationError({"pais": ["Indicá el país."]})
        documento = biblioteca.ndc_de(pais) if pais_con_ndc(pais) else None
        if documento is None:
            raise ValidationError(
                {"pais": [f"No hay una NDC de {pais} en la biblioteca pública: cargala ahí, con la etiqueta NDC y el país."]}
            )

        ficha = next(
            (f for f in FichaNDC.objects.filter(organization=organization) if plegar(f.pais) == plegar(pais)),
            None,
        ) or FichaNDC(organization=organization, pais=pais)
        for campo, valor in campos_de_documento(documento).items():
            setattr(ficha, campo, valor)
        ficha.actualizado_por = request.user
        ficha.error = ""
        ficha.estado = EstadoFicha.GENERANDO
        ficha.save()

        try:
            tasks.despachar_ficha(ficha.pk)
        except Exception as exc:  # noqa: BLE001
            FichaNDC.objects.filter(pk=ficha.pk).update(
                estado=EstadoFicha.ERROR, error=f"No se pudo poner en la cola de trabajo ({exc})."
            )
            ficha.refresh_from_db()
        return Response(serializers.ficha(ficha), status=status.HTTP_201_CREATED)


class FichaView(APIView):
    def delete(self, request, pk: int):
        _ficha(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
