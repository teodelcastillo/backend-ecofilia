"""
La configuración de CAF, en datos (fase 1).

Hasta acá vivía en el código: las etapas y sus lanzamientos en
``apps/project/services/stages.py``, la etiqueta del principal y los grupos en
``apps/document/evidence_tag_groups.py``, los criterios en
``apps/project/services/caf_criteria.py`` y, con opciones y tonos, en
``ecofiliaV2/lib/caf-alineacion.ts``. Esta migración los copia **por valor**,
tal como estaban: una migración tiene que poder correr igual dentro de un año,
aunque esos módulos cambien.

Si no existe la institución ``caf``, no hace nada. Reversible: al volver, CAF
queda sin configuración propia (rige la base).

``CAF_CONFIG`` también lo usan los tests y ``seed_local_instituciones`` para
armar una CAF igual a la de producción.
"""
from django.db import migrations

CAF_SLUG = "caf"

_CONSISTENCIA = [
    {"value": "Consistencia", "short": "Consistente", "tone": "ok"},
    {"value": "Potencialmente inconsistente", "short": "Pot. inconsistente", "tone": "brecha"},
    {"value": "Inconsistente", "short": "Inconsistente", "tone": "critico"},
]

CAF_CONFIG = {
    "version": 1,
    "branding": {"display_name": "CAF", "logo_url": "/images/caf-logo.png"},
    "stages": [
        {"slug": "pre_originacion", "label": "Pre-originación", "principal_tag": None, "launches": []},
        {"slug": "ido_cci", "label": "IDO-CCI", "principal_tag": "ido",
         "launches": ["caf-iet-datbc-evaluacion-tecnica"]},
        {"slug": "dec", "label": "DEC", "principal_tag": "dec", "launches": []},
        {"slug": "aprobada", "label": "Aprobada", "principal_tag": None, "launches": []},
    ],
    "workflows": {
        "report": "caf-iet-datbc-evaluacion-tecnica",
        "guided": "caf-iet-guiado-ejecutivo",
        "reading": "caf-lectura-informe-ejecutivo",
    },
    "evidence_tag_groups": [
        {"slug": "principal", "label": "Documento principal", "tags": ["ido", "dec"]},
        {"slug": "operacion", "label": "Documentos de la operación", "tags": [
            "ias", "analisis-riesgos", "matriz-resultados", "disenos-proyecto",
            "ibat", "otros", "documento-operacion",
        ]},
        {"slug": "contexto", "label": "Contexto país", "tags": [
            "ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap", "pancd",
            "inventario-gei", "ley-cambio-climatico", "reporte-nacional",
        ]},
    ],
    "evaluation": {
        "framework": "caf-datbc",
        "criteria": [
            {"key": "ct_a1", "code": "CT A1", "name": "Análisis de riesgos climáticos",
             "group": "objetivo_1", "is_determination": False, "options": [
                 {"value": "Riesgo bajo", "short": "Riesgo bajo", "tone": "ok"},
                 {"value": "Riesgo medio", "short": "Riesgo medio", "tone": "atencion"},
                 {"value": "Riesgo alto", "short": "Riesgo alto", "tone": "atencion"},
                 {"value": "Indeterminado", "short": "Indeterminado", "tone": "neutro"},
                 {"value": "Sin análisis de riesgos (AR)", "short": "Sin AR", "tone": "pendiente"},
                 {"value": "No aplica análisis de riesgos (AR)", "short": "No aplica AR", "tone": "neutro"},
             ]},
            {"key": "ct_a2", "code": "CT A2", "name": "Análisis de medidas",
             "group": "objetivo_1", "is_determination": False, "options": [
                 {"value": "Suficientes", "short": "Suficientes", "tone": "ok"},
                 {"value": "No suficientes", "short": "Insuficientes", "tone": "brecha"},
                 {"value": "Información insuficiente", "short": "Info. insuficiente", "tone": "pendiente"},
                 {"value": "No aplica", "short": "No aplica", "tone": "neutro"},
             ]},
            {"key": "ct_a3", "code": "CT A3",
             "name": "Consistencia con los instrumentos nacionales de adaptación",
             "group": "objetivo_1", "is_determination": False, "options": _CONSISTENCIA},
            {"key": "objetivo_1", "code": "Objetivo 1", "name": "Desarrollo resiliente al clima",
             "group": None, "is_determination": True, "options": [
                 {"value": "Alineada", "short": "Alineada", "tone": "ok"},
                 {"value": "Con brechas por CT A1", "short": "Brecha A1", "tone": "brecha"},
                 {"value": "Con brechas por CT A2", "short": "Brecha A2", "tone": "brecha"},
                 {"value": "Con brechas por CT A3", "short": "Brecha A3", "tone": "brecha"},
                 {"value": "No alineada", "short": "No alineada", "tone": "critico"},
             ]},
            {"key": "ct_m1", "code": "CT M1", "name": "Actividades consideradas no alineadas",
             "group": "objetivo_2", "is_determination": False, "options": [
                 {"value": "Sin actividades no alineadas", "short": "Sin no alineadas", "tone": "ok"},
                 {"value": "Contiene actividades no alineadas", "short": "Con no alineadas", "tone": "critico"},
             ]},
            {"key": "ct_m2", "code": "CT M2", "name": "Actividades alineadas de manera directa",
             "group": "objetivo_2", "is_determination": False, "options": [
                 {"value": "Contiene actividades alineadas", "short": "Con alineadas", "tone": "ok"},
                 {"value": "Sin actividades alineadas", "short": "Sin alineadas", "tone": "neutro"},
             ]},
            {"key": "ct_m3", "code": "CT M3", "name": "Consistencia con la NDC",
             "group": "objetivo_2", "is_determination": False, "options": _CONSISTENCIA},
            {"key": "ct_m4", "code": "CT M4",
             "name": "Consistencia con trayectorias de desarrollo bajo en emisiones",
             "group": "objetivo_2", "is_determination": False, "options": _CONSISTENCIA},
            {"key": "ct_m5", "code": "CT M5", "name": "Análisis de riesgos de transición",
             "group": "objetivo_2", "is_determination": False, "options": [
                 {"value": "No persisten riesgos de transición", "short": "Sin riesgos", "tone": "ok"},
                 {"value": "Persisten riesgos de transición", "short": "Persisten riesgos", "tone": "brecha"},
             ]},
            {"key": "objetivo_2", "code": "Objetivo 2", "name": "Desarrollo bajo en emisiones de GEI",
             "group": None, "is_determination": True, "options": [
                 {"value": "Alineada en el CT M2", "short": "Alineada M2", "tone": "ok"},
                 {"value": "Alineada en el CT M3", "short": "Alineada M3", "tone": "ok"},
                 {"value": "Alineada en el CT M4", "short": "Alineada M4", "tone": "ok"},
                 {"value": "Alineada en el CT M5", "short": "Alineada M5", "tone": "ok"},
                 {"value": "Con brechas", "short": "Con brechas", "tone": "brecha"},
                 {"value": "No alineada", "short": "No alineada", "tone": "critico"},
             ]},
        ],
    },
}


def aplicar(apps, schema_editor):
    Organization = apps.get_model("user", "Organization")
    db = schema_editor.connection.alias if schema_editor else "default"
    Organization.objects.using(db).filter(slug=CAF_SLUG).update(config=CAF_CONFIG)


def revertir(apps, schema_editor):
    Organization = apps.get_model("user", "Organization")
    db = schema_editor.connection.alias if schema_editor else "default"
    Organization.objects.using(db).filter(slug=CAF_SLUG).update(config={})


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0008_organization_config"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
