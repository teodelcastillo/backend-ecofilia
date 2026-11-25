"""
CAF: lo que todavía estaba escrito para CAF en el front pasa a su configuración.

- **El formulario de la operación** (``operation_form``): país, monto CAF, tipo
  de operación, ejecutivo DATBC, los datos opcionales de la carátula, los
  puntos manuales de la Sección A en DEC, los ecosistemas estratégicos y el
  objetivo y los componentes. Con las mismas listas, los mismos rótulos y el
  mismo orden de la carátula que tenía ``ecofiliaV2/lib/caf-operacion.ts``.
- **Las secciones de su portal** (``portal.sections``), con la Proyección, y los
  cuatro instrumentos que van fijados arriba en su biblioteca
  (``portal.pinned_topics``, antes ``lib/caf-topics.ts``).
- **DEC en preparación** (``stages[].en_preparacion``): el bloqueo que hacía
  ``lib/dec-bloqueado.ts`` desde el 6 de noviembre. Se levanta desde la consola.

Si no existe la institución ``caf`` o no tiene configuración, no hace nada.
Reversible: al volver, le saca las tres cosas y rige lo de la base.
"""
import copy

from django.db import migrations

CAF_SLUG = "caf"
DEC_STAGE = "dec"

PAISES = [
    "Antigua y Barbuda", "Argentina", "Bahamas", "Barbados", "Bolivia", "Brasil", "Chile", "Colombia",
    "Costa Rica", "Ecuador", "El Salvador", "España", "Grenada", "Honduras", "Jamaica", "México", "Panamá",
    "Paraguay", "Perú", "Portugal", "República Dominicana", "Trinidad y Tobago", "Uruguay", "Venezuela",
]

TIPOS_DE_OPERACION = [
    "Programa / Proyecto de Inversión (PPI)",
    "Préstamo programático (PBL)",
    "Préstamo sectorial (SWAp)",
    "Préstamo corporativo",
    "Línea de crédito contingente",
    "Línea de crédito revolvente no comprometida",
    "Línea de crédito comprometida",
    "Financiamiento Extraordinario de Liquidez de corto plazo (FEL)",
    "Financiamiento estructurado",
    "Préstamo A/B",
    "Avales y garantías",
    "Asesoría financiera",
    "Inversión patrimonial",
    "Préstamo subordinado",
    "Autorización de Programa o Facilidad",
]

EJECUTIVOS_DATBC = [
    "Alejandro Miranda", "Cecilia Guerra", "Cristian Grisales", "Erick Castro", "Ignacio Lorenzo",
    "Martha Castillo", "Mauricio Velásquez", "Miguel Guzman", "Nara Vargas", "Octavio Carrasquilla",
    "Oscar Guevara", "René Gómez-García Palao", "Sebastían Rodríguez",
]

ECOSISTEMAS = [
    "Ecosistemas amazónicos",
    "Bosques atlánticos",
    "Mar Caribe",
    "Bosques y sabanas del cerrado",
    "Corredor biológico mesoamericano",
    "Sistema de barrera de arrecifes mesoamericano",
    "Región de surgencia de la Corriente de Humboldt",
    "Ecosistemas de la cuenca del Río de la Plata (incluye el Pantanal)",
    "Andes tropicales",
    "Región Tumbes-Chocó-Magdalena",
]


def _campo(key, label, type="text", **extra):
    """Un campo normalizado, como lo deja ``apps.user.operation_form``."""
    return {
        "key": key,
        "label": label,
        "type": type,
        "required": False,
        "placeholder": "",
        "help": "",
        "options": [],
        "other_option": None,
        "none_option": None,
        "unit": "",
        "report_label": "",
        "wide": False,
        "in_report": True,
        "for_analysis": True,
        "ai": None,
        "default": None,
        **extra,
    }


def _bloque(title, fields, **extra):
    return {
        "title": title,
        "description": "",
        "link": None,
        "stages": [],
        "only_edit": False,
        "collapsible": False,
        "report_title": "",
        "fields": fields,
        **extra,
    }


OPERATION_FORM = {
    "blocks": [
        _bloque(
            "Identidad",
            [
                _campo("pais", "País", "select", required=True, options=PAISES, placeholder="Elegí un país"),
                _campo(
                    "monto",
                    "Monto CAF (millones de USD)",
                    "number",
                    required=True,
                    unit="M USD",
                    report_label="Monto CAF",
                    placeholder="Ej: 150",
                ),
                _campo(
                    "tipo_operacion",
                    "Tipo de operación",
                    "select",
                    required=True,
                    options=TIPOS_DE_OPERACION,
                    other_option="Otra",
                    wide=True,
                    ai="suggest",
                    placeholder="Elegí un tipo",
                ),
                _campo("estado", "Etapa", "etapa", required=True),
                _campo(
                    "ejecutivo_datbc",
                    "Ejecutivo DATBC",
                    "select",
                    required=True,
                    options=EJECUTIVOS_DATBC,
                    other_option="Otro",
                    default="user_name",
                    for_analysis=False,
                    placeholder="Elegí un ejecutivo",
                ),
            ],
        ),
        _bloque(
            "Datos opcionales",
            [
                _campo("fecha_informe", "Fecha del informe", "date", default="today", for_analysis=False),
                _campo(
                    "consecutivo",
                    "Consecutivo",
                    placeholder="DATBC-NAPELLIDO-2026-N.º de informe",
                    for_analysis=False,
                ),
                _campo(
                    "ubicacion_geografica",
                    "Ubicación geográfica",
                    placeholder="Indique la ubicación de la operación si se conoce / si aplica",
                ),
                _campo("representante_pais", "Representante país (ODR)", for_analysis=False),
                _campo("ejecutivo_pais", "Ejecutivo país (ODR)", for_analysis=False),
                _campo("ejecutivo_responsable", "Ejecutivo responsable (AANN)", for_analysis=False),
                _campo("cliente", "Cliente", placeholder="Entidad que recibe el financiamiento"),
                _campo("ejecutor", "Ejecutor", placeholder="Entidad que ejecuta la operación"),
                _campo("link_focus", "Link Focus", "url", wide=True, placeholder="https://…", for_analysis=False),
            ],
            collapsible=True,
        ),
        _bloque(
            "Antecedentes y Misión de Evaluación",
            [
                _campo("mision_evaluacion", "Misión de Evaluación", "textarea"),
                _campo("antecedentes", "Antecedentes para el análisis (progreso con respecto al IDO)", "textarea"),
            ],
            description="Se incluyen en la Sección A del informe.",
            stages=[DEC_STAGE],
        ),
        _bloque(
            "Ecosistemas estratégicos",
            [
                _campo(
                    "ecosistemas_estrategicos",
                    "Ecosistemas estratégicos",
                    "multiselect",
                    options=ECOSISTEMAS,
                    other_option="Otros",
                    none_option="Ninguno de los anteriores",
                ),
            ],
            description="Marcá todos los que correspondan. Según la ubicación de la operación, si se conoce.",
            link={
                "label": "Consulte el Mapa de Ecosistemas Estratégicos",
                "href": "https://public.tableau.com/app/profile/joaquin.de.la.fuente3537/viz/HerramientaDigitalCOP16/HDCOP",
            },
        ),
        _bloque(
            "Contenido de la operación",
            [
                _campo(
                    "objetivo",
                    "Objetivo",
                    "textarea",
                    ai="fill",
                    placeholder="Describí el objetivo principal de la operación…",
                ),
                _campo(
                    "componentes",
                    "Componentes / Actividades",
                    "textarea",
                    ai="fill",
                    report_label="Componentes y actividades",
                    placeholder="Listá los componentes o actividades principales de la operación…",
                ),
            ],
            only_edit=True,
            report_title="Objetivo y componentes de la operación",
        ),
    ],
    # El orden de la plantilla del IET. Número, sector y modalidad no son
    # campos: los traen operaciones viejas y salen sólo si tienen valor.
    "caratula": [
        {"key": "numero_operacion", "label": "Número de operación"},
        {"key": "estado", "label": "Estado de avance"},
        {"key": "tipo_operacion"},
        {"key": "fecha_informe"},
        {"key": "consecutivo"},
        {"key": "pais"},
        {"key": "ubicacion_geografica"},
        {"key": "representante_pais"},
        {"key": "ejecutivo_pais"},
        {"key": "ejecutivo_responsable"},
        {"key": "ejecutivo_datbc"},
        {"key": "link_focus"},
        {"key": "sector", "label": "Sector"},
        {"key": "cliente"},
        {"key": "ejecutor"},
        {"key": "monto"},
        {"key": "modalidad", "label": "Modalidad"},
        {"key": "ecosistemas_estrategicos"},
    ],
}

PORTAL = {
    "sections": ["biblioteca", "operaciones", "informes", "tablero", "proyeccion"],
    "pinned_topics": [
        {"acronym": "ndcs", "label": "NDCS: CONTRIBUCIONES DETERMINADAS A NIVEL NACIONAL"},
        {"acronym": "naps", "label": "NAPS: PLANES NACIONALES DE ADAPTACIÓN"},
        {"acronym": "lts", "label": "LTS: ESTRATEGIAS DE DESARROLLO A LARGO PLAZO CON BAJAS EMISIONES DE GEI"},
        {"acronym": "ac", "label": "AC: COMUNICACIONES DE ADAPTACIÓN"},
    ],
}


def _caf(apps):
    Organization = apps.get_model("user", "Organization")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None or not caf.config:
        return None
    return caf


def aplicar(apps, schema_editor):
    caf = _caf(apps)
    if caf is None:
        return
    config = copy.deepcopy(caf.config)
    config["operation_form"] = copy.deepcopy(OPERATION_FORM)
    config["portal"] = copy.deepcopy(PORTAL)
    for stage in config.get("stages") or []:
        if stage.get("slug") == DEC_STAGE:
            stage["en_preparacion"] = True
    caf.config = config
    caf.save(update_fields=["config"])


def revertir(apps, schema_editor):
    caf = _caf(apps)
    if caf is None:
        return
    config = copy.deepcopy(caf.config)
    config.pop("operation_form", None)
    config.pop("portal", None)
    for stage in config.get("stages") or []:
        stage.pop("en_preparacion", None)
    caf.config = config
    caf.save(update_fields=["config"])


class Migration(migrations.Migration):
    dependencies = [("user", "0017_caf_dec_hereda_contexto")]

    operations = [migrations.RunPython(aplicar, revertir)]
