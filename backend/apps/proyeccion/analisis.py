"""El screening de una proyección: fichas NDC, semáforo por operación y resumen.

Pensado para que sea rápido aunque el pipeline sea grande:

- las operaciones idénticas (mismo país, cliente, descripción y producto) se
  analizan una sola vez;
- van al modelo en lotes, agrupadas por país, y los lotes corren en paralelo;
- la ficha NDC de cada país se arma una vez y se reutiliza todos los meses.

El módulo sólo habla con Claude. No sigue al resto de la plataforma si alguna
vez cambia de proveedor ni acepta un modelo de otro: un id que no sea de Claude
es un error de configuración y el análisis falla diciéndolo, en vez de correr
con otro modelo sin que nadie lo note.

Los hilos sólo hablan con el modelo: todo lo que toca la base se hace en el
hilo que lanzó el análisis, a medida que los lotes vuelven.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from django.utils import timezone

from apps.document.utils.llm import (
    ROLE_BALANCED,
    anthropic_chat_completion,
    anthropic_structured_completion,
    is_anthropic_model,
    resolve_model,
)
from apps.proyeccion import prompts
from apps.proyeccion.lectura import en_pipeline, plegar
from apps.proyeccion.models import (
    AlcanceNDC,
    EstadoAnalisis,
    EstadoFicha,
    FichaNDC,
    FuenteFicha,
    Nivel,
    OperacionProyectada,
    Proyeccion,
)

logger = logging.getLogger(__name__)

TAMANO_LOTE = int(os.environ.get("PROYECCION_TAMANO_LOTE", "15"))
HILOS = int(os.environ.get("PROYECCION_HILOS", "4"))
_TIMEOUT = 240.0
_MAX_CARACTERES_NDC = 400_000
DIAS_PROXIMA = 60

# Países de la planilla que no son un país con NDC propia.
_SIN_NDC = {"regional", "multipais", "multinacional", "varios", "global", ""}

_CAMPOS_SCREENING = [
    "analizada",
    "tipo_operacion",
    "no_alineadas",
    "nota_no_alineadas",
    "riesgo_transicion",
    "riesgo_categoria",
    "nota_riesgo",
    "oportunidad_ndc",
    "nota_oportunidad",
    "brecha_ndc",
    "nota_brecha",
    "fuente_base",
    "fuente_ndc",
]

_EN_BLANCO = {
    "analizada": False,
    "tipo_operacion": "",
    "no_alineadas": "",
    "nota_no_alineadas": "",
    "riesgo_transicion": "",
    "riesgo_categoria": None,
    "nota_riesgo": "",
    "oportunidad_ndc": "",
    "nota_oportunidad": "",
    "brecha_ndc": "",
    "nota_brecha": "",
    "fuente_base": "",
    "fuente_ndc": "",
}


# El modelo de Claude del módulo cuando la plataforma no fija uno.
MODELO_POR_DEFECTO = "claude-sonnet-5"


def modelo_del_screening() -> str:
    """El modelo de Claude con el que corre el módulo. Nunca uno de otro proveedor.

    ``PROYECCION_MODEL`` lo fija a mano y tiene que ser de Claude. Sin esa
    variable se usa el nivel intermedio de la plataforma si es de Claude
    (``LLM_PROVIDER=anthropic``, como en producción) y, si no lo es,
    ``MODELO_POR_DEFECTO``.
    """
    elegido = (os.environ.get("PROYECCION_MODEL") or "").strip()
    if elegido:
        if not is_anthropic_model(elegido):
            raise RuntimeError(
                f"PROYECCION_MODEL={elegido} no es un modelo de Claude; el módulo de proyección sólo usa Claude"
            )
        return elegido
    de_la_plataforma = resolve_model(ROLE_BALANCED)
    return de_la_plataforma if is_anthropic_model(de_la_plataforma) else MODELO_POR_DEFECTO


# ---------------------------------------------------------------------------
# Llamadas al modelo
# ---------------------------------------------------------------------------


def _solo_claude(modelo: str) -> None:
    if not is_anthropic_model(modelo):
        raise RuntimeError(f"{modelo} no es un modelo de Claude; el módulo de proyección sólo usa Claude")


def _completar_json(mensajes: list[dict], esquema: dict, *, modelo: str, max_tokens: int) -> tuple[dict, dict]:
    """Una respuesta en JSON contra el esquema, con un reintento. Es el punto que los tests reemplazan."""
    _solo_claude(modelo)
    ultimo: Exception | None = None
    for intento in (1, 2):
        try:
            return anthropic_structured_completion(
                mensajes, model=modelo, schema=esquema, max_tokens=max_tokens, timeout=_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 — se informa arriba
            ultimo = exc
            logger.warning("proyeccion: intento %s falló (%s): %s", intento, modelo, exc)
    raise RuntimeError(str(ultimo)) from ultimo


def _completar_texto(mensajes: list[dict], *, modelo: str, max_tokens: int) -> tuple[str, dict]:
    _solo_claude(modelo)
    return anthropic_chat_completion(mensajes, model=modelo, max_tokens=max_tokens, timeout=_TIMEOUT)


def motivo(exc: Exception) -> str:
    """El error del modelo, dicho para quien mira la pantalla."""
    texto = str(exc)
    plano = texto.lower()
    if "credit balance" in plano or "insufficient_quota" in plano or "billing" in plano:
        return "la cuenta del proveedor del modelo de IA no tiene crédito disponible"
    if "rate limit" in plano or "rate_limit" in plano or "overloaded" in plano:
        return "el proveedor del modelo de IA está saturado; probá de nuevo en unos minutos"
    if "timed out" in plano or "timeout" in plano:
        return "el modelo de IA tardó demasiado en responder"
    return texto[:300]


def _sumar_uso(total: dict, uso: dict | None) -> None:
    for clave in ("input_tokens", "output_tokens"):
        total[clave] = int(total.get(clave, 0)) + int((uso or {}).get(clave, 0) or 0)


# ---------------------------------------------------------------------------
# Fichas NDC
# ---------------------------------------------------------------------------

_ESQUEMA_FICHA = {
    "type": "object",
    "properties": {
        "conoce": {"type": "boolean"},
        "version": {"type": "string"},
        "contenido": {"type": "string"},
    },
    "required": ["conoce", "version", "contenido"],
    "additionalProperties": False,
}


def pais_con_ndc(pais: str) -> bool:
    return plegar(pais) not in _SIN_NDC


def redactar_ficha(pais: str, texto_ndc: str = "", *, modelo: str | None = None) -> dict:
    """La ficha de un país, desde la NDC adjunta o desde conocimiento general.

    Devuelve ``estado``, ``fuente``, ``version_ndc``, ``contenido`` y ``uso``.
    Sólo habla con el modelo: guardar es cosa de quien llama.
    """
    modelo = modelo or modelo_del_screening()
    texto_ndc = (texto_ndc or "").strip()
    if texto_ndc:
        fuente = FuenteFicha.NDC
        pedido = (
            f"<ndc>\n{texto_ndc[:_MAX_CARACTERES_NDC]}\n</ndc>\n\n"
            + prompts.INSTRUCCIONES_FICHA.format(pais=pais, origen=prompts.ORIGEN_FICHA_ADJUNTA)
        )
    else:
        fuente = FuenteFicha.CONOCIMIENTO_GENERAL
        pedido = prompts.INSTRUCCIONES_FICHA.format(pais=pais, origen=prompts.ORIGEN_FICHA_CONOCIMIENTO)
    mensajes = [
        {
            "role": "system",
            "content": (
                "Eres un analista técnico de clima de un banco de desarrollo. Resumes NDC "
                "de países con precisión y sin agregar nada que el documento no diga."
            ),
        },
        {"role": "user", "content": pedido},
    ]
    respuesta, uso = _completar_json(mensajes, _ESQUEMA_FICHA, modelo=modelo, max_tokens=2500)
    contenido = str(respuesta.get("contenido") or "").strip()
    conoce = bool(respuesta.get("conoce")) and bool(contenido)
    return {
        "estado": EstadoFicha.LISTA if conoce else EstadoFicha.SIN_DATOS,
        "fuente": fuente,
        "version_ndc": str(respuesta.get("version") or "").strip()[:255] if conoce else "",
        "contenido": contenido if conoce else "",
        "uso": uso,
    }


def generar_ficha(ficha_id: int) -> None:
    """Completa una ficha que quedó en ``generando`` (la dispara la API)."""
    try:
        ficha = FichaNDC.objects.get(pk=ficha_id)
    except FichaNDC.DoesNotExist:
        return
    if ficha.estado != EstadoFicha.GENERANDO:
        return
    try:
        resultado = redactar_ficha(ficha.pais, ficha.texto_ndc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("proyeccion: no se pudo armar la ficha NDC de %s", ficha.pais)
        FichaNDC.objects.filter(pk=ficha_id).update(
            estado=EstadoFicha.ERROR,
            error=f"No se pudo armar la ficha: {motivo(exc)}.",
            actualizado_en=timezone.now(),
        )
        return
    FichaNDC.objects.filter(pk=ficha_id).update(
        estado=resultado["estado"],
        fuente=resultado["fuente"],
        version_ndc=resultado["version_ndc"],
        contenido=resultado["contenido"],
        error="",
        actualizado_en=timezone.now(),
    )


def texto_de_ndc(nombre: str, contenido: bytes) -> str:
    """El texto de la NDC que se adjunta: PDF o texto plano."""
    extension = (nombre or "").lower().rsplit(".", 1)[-1]
    if extension in ("txt", "md"):
        for codificacion in ("utf-8-sig", "cp1252"):
            try:
                return contenido.decode(codificacion)
            except UnicodeDecodeError:
                continue
        return contenido.decode("latin-1", errors="replace")
    if extension != "pdf":
        raise ValueError("Subí la NDC como PDF o texto (.txt).")
    import fitz  # pymupdf

    try:
        documento = fitz.open(stream=contenido, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("No se pudo abrir el PDF.") from exc
    with documento:
        partes: list[str] = []
        total = 0
        for pagina in documento:
            texto = pagina.get_text()
            partes.append(texto)
            total += len(texto)
            if total > _MAX_CARACTERES_NDC:
                break
    texto = "\n".join(partes).replace("\x00", "").strip()
    if len(texto) < 500:
        raise ValueError("El PDF no tiene texto legible (¿está escaneado?).")
    return texto


# ---------------------------------------------------------------------------
# Screening por lotes
# ---------------------------------------------------------------------------

_ALERTA_RIESGO = ["NINGUNA", Nivel.ROJO, Nivel.AMARILLO, Nivel.SIN_INFO]

_ESQUEMA_SCREENING = {
    "type": "object",
    "properties": {
        "operaciones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tipo_operacion": {"type": "string", "enum": list(prompts.TIPOS_DE_OPERACION)},
                    "no_alineadas": {"type": "string", "enum": _ALERTA_RIESGO},
                    "nota_no_alineadas": {"type": "string"},
                    "riesgo_transicion": {"type": "string", "enum": _ALERTA_RIESGO},
                    "riesgo_categoria": {"type": "integer"},
                    "nota_riesgo": {"type": "string"},
                    "oportunidad_ndc": {"type": "string", "enum": ["NINGUNA", Nivel.VERDE, Nivel.POSIBLE]},
                    "nota_oportunidad": {"type": "string"},
                    "brecha_ndc": {"type": "string", "enum": ["NINGUNA", Nivel.ROJO, Nivel.AMARILLO]},
                    "nota_brecha": {"type": "string"},
                    "usa_conocimiento_general": {"type": "boolean"},
                },
                "required": [
                    "id",
                    "tipo_operacion",
                    "no_alineadas",
                    "nota_no_alineadas",
                    "riesgo_transicion",
                    "riesgo_categoria",
                    "nota_riesgo",
                    "oportunidad_ndc",
                    "nota_oportunidad",
                    "brecha_ndc",
                    "nota_brecha",
                    "usa_conocimiento_general",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["operaciones"],
    "additionalProperties": False,
}

_FUENTE_DE_FICHA = {
    FuenteFicha.NDC: "NDC adjunta",
    FuenteFicha.MANUAL: "ficha cargada por el equipo",
    FuenteFicha.CONOCIMIENTO_GENERAL: "conocimiento general, a validar",
}


def _clave_de_analisis(op: OperacionProyectada) -> tuple[str, str, str, str]:
    return (plegar(op.pais), plegar(op.cliente), plegar(op.descripcion), plegar(op.producto))


def _mensajes_del_lote(lote: list[OperacionProyectada], fichas: dict[str, FichaNDC]) -> list[dict]:
    paises = []
    for op in lote:
        clave = plegar(op.pais)
        if clave in fichas and clave not in paises:
            paises.append(clave)
    if paises:
        bloques = [
            f"## {fichas[clave].pais} (fuente: {_FUENTE_DE_FICHA.get(fichas[clave].fuente, 'ficha')})\n"
            f"{fichas[clave].version_ndc}\n{fichas[clave].contenido}".strip()
            for clave in paises
        ]
        seccion_fichas = "<fichas_ndc>\n" + "\n\n".join(bloques) + "\n</fichas_ndc>"
    else:
        seccion_fichas = (
            "No se incluyen fichas NDC para estas operaciones: deja las alertas 3 y 4 en NINGUNA."
        )
    operaciones = [
        {
            "id": op.codigo,
            "pais": op.pais,
            "cliente": op.cliente,
            "descripcion": op.descripcion[:600],
            "producto": op.producto,
            "monto_usd_mm": float(op.monto_usd_mm) if op.monto_usd_mm is not None else None,
        }
        for op in lote
    ]
    pedido = (
        f"{seccion_fichas}\n\n<operaciones>\n{json.dumps(operaciones, ensure_ascii=False, indent=1)}\n"
        f"</operaciones>\n\n{prompts.INSTRUCCIONES_SCREENING}"
    )
    return [
        {"role": "system", "content": prompts.INSTRUCCIONES_GENERALES},
        {"role": "user", "content": pedido},
    ]


def _nivel(valor, permitidos: tuple[str, ...]) -> str:
    texto = str(valor or "").strip().upper()
    return texto if texto in permitidos else ""


def aplicar_resultado(op: OperacionProyectada, resultado: dict, ficha: FichaNDC | None) -> None:
    """Vuelca en la operación lo que devolvió el modelo, ya validado.

    Lo que el modelo no puede decidir no se le cree: un nivel fuera de la
    lista queda vacío, una nota sin alerta se descarta, y sin ficha NDC del
    país no hay alertas 3 ni 4, diga lo que diga la respuesta.
    """
    tipo = str(resultado.get("tipo_operacion") or "").strip()
    op.tipo_operacion = tipo if tipo in prompts.TIPOS_DE_OPERACION and tipo != "No identificable" else ""

    op.no_alineadas = _nivel(resultado.get("no_alineadas"), Nivel.RIESGO)
    op.nota_no_alineadas = str(resultado.get("nota_no_alineadas") or "").strip() if op.no_alineadas else ""

    op.riesgo_transicion = _nivel(resultado.get("riesgo_transicion"), Nivel.RIESGO)
    op.nota_riesgo = str(resultado.get("nota_riesgo") or "").strip() if op.riesgo_transicion else ""
    categoria = resultado.get("riesgo_categoria")
    op.riesgo_categoria = (
        categoria
        if op.riesgo_transicion in (Nivel.ROJO, Nivel.AMARILLO) and isinstance(categoria, int) and 1 <= categoria <= 4
        else None
    )

    if ficha is not None:
        op.oportunidad_ndc = _nivel(resultado.get("oportunidad_ndc"), Nivel.OPORTUNIDAD)
        op.brecha_ndc = _nivel(resultado.get("brecha_ndc"), Nivel.BRECHA)
    else:
        op.oportunidad_ndc = ""
        op.brecha_ndc = ""
    op.nota_oportunidad = str(resultado.get("nota_oportunidad") or "").strip() if op.oportunidad_ndc else ""
    op.nota_brecha = str(resultado.get("nota_brecha") or "").strip() if op.brecha_ndc else ""

    hay_base = op.no_alineadas in (Nivel.ROJO, Nivel.AMARILLO) or op.riesgo_transicion in (
        Nivel.ROJO,
        Nivel.AMARILLO,
    )
    op.fuente_base = (
        ("conocimiento_general" if resultado.get("usa_conocimiento_general") else "excel") if hay_base else ""
    )
    op.fuente_ndc = ficha.fuente if ficha is not None and (op.oportunidad_ndc or op.brecha_ndc) else ""
    op.analizada = True


def _copiar_resultado(origen: OperacionProyectada, destino: OperacionProyectada) -> None:
    for campo in _CAMPOS_SCREENING:
        setattr(destino, campo, getattr(origen, campo))


# ---------------------------------------------------------------------------
# Resumen del mes
# ---------------------------------------------------------------------------

MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)  # fmt: skip


def nombre_del_mes(periodo: date) -> str:
    return f"{MESES[periodo.month - 1]} de {periodo.year}"


def es_proxima(fecha: date | None, hoy: date | None = None) -> bool:
    hoy = hoy or timezone.localdate()
    return bool(fecha and hoy <= fecha <= hoy + timedelta(days=DIAS_PROXIMA))


def tiene_alerta(op: OperacionProyectada) -> bool:
    """Alguna alerta u oportunidad. «S/I» no lo es: dice que no hay con qué evaluar."""
    senales = (Nivel.ROJO, Nivel.AMARILLO)
    return bool(
        op.no_alineadas in senales or op.riesgo_transicion in senales or op.oportunidad_ndc or op.brecha_ndc
    )


def sin_informacion(op: OperacionProyectada) -> bool:
    return Nivel.SIN_INFO in (op.no_alineadas, op.riesgo_transicion) and not tiene_alerta(op)


def _linea_para_resumen(op: OperacionProyectada) -> str:
    partes = [op.codigo, op.pais, op.cliente[:90], op.descripcion[:140]]
    if op.no_alineadas in (Nivel.ROJO, Nivel.AMARILLO):
        partes.append(f"No alineadas: {op.no_alineadas} ({op.nota_no_alineadas})")
    if op.riesgo_transicion in (Nivel.ROJO, Nivel.AMARILLO):
        categoria = f" Cat. {op.riesgo_categoria}" if op.riesgo_categoria else ""
        partes.append(f"Riesgo de transición: {op.riesgo_transicion}{categoria} ({op.nota_riesgo})")
    if op.oportunidad_ndc:
        partes.append(f"Oportunidad NDC: {op.oportunidad_ndc} ({op.nota_oportunidad})")
    if op.brecha_ndc:
        partes.append(f"Brecha NDC: {op.brecha_ndc} ({op.nota_brecha})")
    if op.nueva:
        partes.append("Nueva este mes")
    return " | ".join(partes)


def redactar_resumen(
    proyeccion: Proyeccion, operaciones: list[OperacionProyectada], *, modelo: str, con_ndc: bool
) -> tuple[str, dict]:
    analizadas = [op for op in operaciones if op.analizada]
    con_alerta = [op for op in analizadas if tiene_alerta(op)]
    hay_comparacion = any(op.nueva is not None for op in analizadas)
    nuevas = sum(1 for op in analizadas if op.nueva)
    encabezado = [
        f"Mes: {nombre_del_mes(proyeccion.periodo)}",
        f"Operaciones revisadas: {len(analizadas)}",
        f"Operaciones con al menos una alerta u oportunidad: {len(con_alerta)}",
        f"Operaciones sin información suficiente para identificar el sector (S/I): "
        f"{sum(1 for op in analizadas if sin_informacion(op))}",
        f"Operaciones nuevas respecto del mes anterior: {nuevas}"
        if hay_comparacion
        else "No hay un mes anterior cargado para identificar operaciones nuevas.",
        # La cuenta de fechas no se le deja al modelo: la lista viene hecha.
        "Operaciones con alerta y CCI/IDO en los próximos 60 días: "
        + (
            "; ".join(f"{op.cliente[:60]} ({op.fecha_cci_ido:%d/%m/%Y})" for op in con_alerta if es_proxima(op.fecha_cci_ido))
            or "ninguna"
        ),
    ]
    sin_ndc = (
        ""
        if con_ndc
        else '\n   Este mes no se aplicaron las alertas de NDC: en esas dos viñetas escribe "No evaluadas este mes".'
    )
    datos = "\n".join(_linea_para_resumen(op) for op in con_alerta) or "(Ninguna operación con alertas.)"
    pedido = (
        "<screening>\n" + "\n".join(encabezado) + "\n\nOperaciones con alertas:\n" + datos + "\n</screening>\n\n"
        + prompts.INSTRUCCIONES_RESUMEN.format(mes=nombre_del_mes(proyeccion.periodo), sin_ndc=sin_ndc)
    )
    mensajes = [
        {"role": "system", "content": prompts.INSTRUCCIONES_GENERALES},
        {"role": "user", "content": pedido},
    ]
    texto, uso = _completar_texto(mensajes, modelo=modelo, max_tokens=3000)
    return texto.strip(), uso


# ---------------------------------------------------------------------------
# El análisis completo
# ---------------------------------------------------------------------------


def operaciones_en_alcance(proyeccion: Proyeccion) -> list[OperacionProyectada]:
    """Las operaciones que entran al análisis según el alcance pedido."""
    operaciones = list(proyeccion.operaciones.all())
    estados = (proyeccion.alcance or {}).get("statuses")
    if isinstance(estados, list) and estados:
        elegidos = set(estados)
        return [op for op in operaciones if op.status in elegidos]
    return [op for op in operaciones if en_pipeline(op.status)]


def _latido(proyeccion_id: int, **campos) -> None:
    Proyeccion.objects.filter(pk=proyeccion_id).update(latido=timezone.now(), **campos)


def _preparar_fichas(
    proyeccion: Proyeccion, operaciones: list[OperacionProyectada], *, modelo: str, uso: dict, avisos: list[str]
) -> dict[str, FichaNDC]:
    """Las fichas utilizables por país (clave plegada), armando las que falten si se pidió."""
    alcance = (proyeccion.alcance or {}).get("ndc") or AlcanceNDC.TODOS
    if alcance == AlcanceNDC.NINGUNO:
        return {}

    paises: dict[str, str] = {}
    for op in operaciones:
        if pais_con_ndc(op.pais):
            paises.setdefault(plegar(op.pais), op.pais)

    existentes = {plegar(f.pais): f for f in FichaNDC.objects.filter(organization=proyeccion.organization)}
    if alcance == AlcanceNDC.TODOS:
        faltan = [(clave, pais) for clave, pais in paises.items() if clave not in existentes]
        if faltan:
            _latido(proyeccion.pk, etapa="fichas", progreso_hechas=0, progreso_total=len(faltan))
            hechas = 0
            fallidas: list[str] = []
            with ThreadPoolExecutor(max_workers=HILOS) as hilos:
                pendientes = {hilos.submit(redactar_ficha, pais, "", modelo=modelo): (clave, pais) for clave, pais in faltan}
                for futuro in as_completed(pendientes):
                    clave, pais = pendientes[futuro]
                    hechas += 1
                    try:
                        resultado = futuro.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("proyeccion: ficha NDC de %s falló: %s", pais, exc)
                        fallidas.append(pais)
                        _latido(proyeccion.pk, progreso_hechas=hechas)
                        continue
                    _sumar_uso(uso, resultado["uso"])
                    ficha, _ = FichaNDC.objects.update_or_create(
                        organization=proyeccion.organization,
                        pais=pais,
                        defaults={
                            "estado": resultado["estado"],
                            "fuente": resultado["fuente"],
                            "version_ndc": resultado["version_ndc"],
                            "contenido": resultado["contenido"],
                            "error": "",
                        },
                    )
                    existentes[clave] = ficha
                    _latido(proyeccion.pk, progreso_hechas=hechas)
            if fallidas:
                avisos.append(
                    "No se pudo armar la ficha NDC de: " + ", ".join(sorted(fallidas)) + ". "
                    "Sus operaciones quedaron sin alertas de NDC."
                )
    return {clave: ficha for clave, ficha in existentes.items() if clave in paises and ficha.utilizable}


def _analizar(proyeccion: Proyeccion) -> None:
    modelo = modelo_del_screening()
    uso: dict = {}
    avisos: list[str] = []

    operaciones = operaciones_en_alcance(proyeccion)
    if not operaciones:
        Proyeccion.objects.filter(pk=proyeccion.pk).update(
            estado=EstadoAnalisis.ERROR,
            error="No hay operaciones para analizar con el alcance elegido.",
            latido=timezone.now(),
        )
        return

    fichas = _preparar_fichas(proyeccion, operaciones, modelo=modelo, uso=uso, avisos=avisos)

    # Las operaciones idénticas se analizan una vez.
    grupos: dict[tuple, list[OperacionProyectada]] = {}
    for op in operaciones:
        grupos.setdefault(_clave_de_analisis(op), []).append(op)
    representantes = sorted((grupo[0] for grupo in grupos.values()), key=lambda op: (plegar(op.pais), op.orden))
    lotes = [representantes[i : i + TAMANO_LOTE] for i in range(0, len(representantes), TAMANO_LOTE)]

    _latido(proyeccion.pk, etapa="screening", progreso_hechas=0, progreso_total=len(operaciones))
    hechas = 0
    sin_analizar = 0
    ultimo_error = ""
    resueltas: set[int] = set()

    def correr(lote: list[OperacionProyectada]) -> tuple[dict, dict]:
        # Un lote grande son ~200 tokens por operación; el tope va holgado.
        return _completar_json(
            _mensajes_del_lote(lote, fichas), _ESQUEMA_SCREENING, modelo=modelo, max_tokens=400 * len(lote) + 1000
        )

    with ThreadPoolExecutor(max_workers=HILOS) as hilos:
        pendientes = {hilos.submit(correr, lote): lote for lote in lotes}
        for futuro in as_completed(pendientes):
            lote = pendientes[futuro]
            afectadas: list[OperacionProyectada] = []
            try:
                respuesta, uso_lote = futuro.result()
                _sumar_uso(uso, uso_lote)
                por_id = {
                    str(item.get("id")): item
                    for item in respuesta.get("operaciones") or []
                    if isinstance(item, dict)
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("proyeccion: un lote de %s operaciones falló: %s", len(lote), exc)
                ultimo_error = str(exc)
                por_id = {}
            for representante in lote:
                grupo = grupos[_clave_de_analisis(representante)]
                resultado = por_id.get(representante.codigo)
                if resultado is None:
                    sin_analizar += len(grupo)
                    continue
                aplicar_resultado(representante, resultado, fichas.get(plegar(representante.pais)))
                for otra in grupo[1:]:
                    _copiar_resultado(representante, otra)
                afectadas.extend(grupo)
            if afectadas:
                OperacionProyectada.objects.bulk_update(afectadas, _CAMPOS_SCREENING)
                resueltas.update(op.pk for op in afectadas)
            hechas += sum(len(grupos[_clave_de_analisis(op)]) for op in lote)
            _latido(proyeccion.pk, progreso_hechas=hechas)

    if sin_analizar == len(operaciones):
        # Nada se analizó: no es un resultado con huecos, es un análisis que
        # falló. El análisis anterior, si lo había, queda como estaba.
        raise RuntimeError(ultimo_error or "el modelo no devolvió ningún resultado")
    # Un análisis nuevo pisa al anterior entero: lo que quedó fuera del alcance
    # o sin respuesta no conserva resultados viejos mezclados con los nuevos.
    proyeccion.operaciones.exclude(pk__in=resueltas).update(**_EN_BLANCO)
    for op in operaciones:
        if op.pk not in resueltas:
            for campo, valor in _EN_BLANCO.items():
                setattr(op, campo, valor)
    if sin_analizar:
        avisos.append(
            f"{sin_analizar} operaciones no se pudieron analizar (el modelo no respondió por ellas). "
            "Volvé a lanzar el análisis para completarlas."
        )

    _latido(proyeccion.pk, etapa="resumen")
    resumen = ""
    try:
        resumen, uso_resumen = redactar_resumen(proyeccion, operaciones, modelo=modelo, con_ndc=bool(fichas))
        _sumar_uso(uso, uso_resumen)
    except Exception as exc:  # noqa: BLE001
        logger.exception("proyeccion: no se pudo redactar el resumen")
        avisos.append(f"No se pudo redactar el resumen del mes: {motivo(exc)}.")

    Proyeccion.objects.filter(pk=proyeccion.pk).update(
        estado=EstadoAnalisis.LISTO,
        etapa="",
        resumen=resumen,
        aviso=" ".join(avisos),
        error="",
        modelo=modelo,
        uso=uso,
        analizado_en=timezone.now(),
        latido=timezone.now(),
    )


def analizar(proyeccion_id: int) -> None:
    """Corre el análisis de una proyección que está en cola.

    Tomarla es un UPDATE atómico: si el mensaje llega dos veces (una
    reentrega de la cola), la segunda no encuentra nada que tomar.
    """
    tomada = Proyeccion.objects.filter(pk=proyeccion_id, estado=EstadoAnalisis.EN_COLA).update(
        estado=EstadoAnalisis.ANALIZANDO, etapa="preparando", latido=timezone.now(), error="", aviso=""
    )
    if not tomada:
        return
    proyeccion = Proyeccion.objects.select_related("organization").get(pk=proyeccion_id)
    try:
        _analizar(proyeccion)
    except Exception as exc:  # noqa: BLE001
        logger.exception("proyeccion: el análisis de %s falló", proyeccion_id)
        Proyeccion.objects.filter(pk=proyeccion_id).update(
            estado=EstadoAnalisis.ERROR,
            etapa="",
            error=f"El análisis no se pudo completar: {motivo(exc)}.",
            latido=timezone.now(),
        )
