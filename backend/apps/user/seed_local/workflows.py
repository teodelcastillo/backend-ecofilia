"""
Los workflows de CAF tal como están en producción, para la base local.

En producción el staff los ajusta desde el Studio, así que las migraciones ya no
los describen: la IET de IDO tiene otros títulos («D.1.1. … (CT A1)», E.1/E.2/E.3,
S.1/S.2), el guiado la sigue, y la IET de DEC
(``dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion``) no la siembra
ninguna migración. Cada JSON de ``workflows/`` es la respuesta de
``GET /api/skills/<slug>/`` de producción, sin dueño ni ids. Para refrescarlos,
ver «Prueba local» en ``backend/docs/contrato-instituciones.md``.

Excepción: la IET de DEC ya tiene la estructura propuesta en ``backend/docs/workflow-dec.md``
(los mismos códigos que IDO y el contexto B.1–B.5 primero), que todavía no está en producción.
Refrescarla desde producción la vuelve a la estructura vieja.

Se cargan con ``update_or_create`` por slug, y los pasos por posición: las ids
de los pasos que ya existían no cambian, así que correr el seed de nuevo no
rompe las corridas que apuntan a ellos.
"""
from __future__ import annotations

import json
from pathlib import Path

from apps.skill.definition import PARAMETER_FIELDS, SKILL_FIELDS, STEP_FIELDS
from apps.skill.models import Skill, SkillParameter, SkillScope, SkillStep

WORKFLOWS_DIR = Path(__file__).resolve().parent / "workflows"

IET_IDO = "caf-iet-datbc-evaluacion-tecnica"
IET_DEC = "dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion"
GUIADO = "caf-iet-guiado-ejecutivo"
SLUGS = (IET_IDO, GUIADO, IET_DEC)

# Además de la definición: dónde se puede correr y quién.
EXTRA_SKILL_FIELDS = ("allowed_contexts", "members_can_run")
# Lo que la API devuelve y no se guarda en el JSON (ni se carga).
CAMPOS_QUE_NO_VAN = (
    "id", "owner", "owner_email", "created_at", "updated_at",
    "organization_slugs", "default_organization_slugs",
)


def definicion(slug: str) -> dict:
    with open(WORKFLOWS_DIR / f"{slug}.json", encoding="utf-8") as fh:
        return json.load(fh)


def limpiar(api_json: dict) -> dict:
    """La respuesta de la API lista para guardar en ``workflows/``: sin dueño ni ids."""
    out = {k: v for k, v in api_json.items() if k not in CAMPOS_QUE_NO_VAN}
    # Lo único que interesa de a quién está asignado: si CAF lo tiene por defecto.
    out["default_en_institucion"] = bool(api_json.get("default_organization_slugs"))
    out["steps"] = [{k: v for k, v in step.items() if k != "id"} for step in api_json.get("steps", [])]
    out["parameters"] = [{k: v for k, v in p.items() if k != "id"} for p in api_json.get("parameters", [])]
    return out


def cargar(slug: str, organization) -> Skill:
    data = definicion(slug)
    defaults = {f: data[f] for f in SKILL_FIELDS + EXTRA_SKILL_FIELDS if f in data}
    defaults.update(scope=SkillScope.ORGANIZATION, organization=organization, owner=None)
    skill, _ = Skill.objects.update_or_create(slug=slug, defaults=defaults)

    posiciones = []
    for paso in data.get("steps", []):
        valores = {f: paso[f] for f in STEP_FIELDS if f in paso and f != "position"}
        linked = paso.get("linked_skill_slug")
        valores["linked_skill"] = Skill.objects.filter(slug=linked).first() if linked else None
        SkillStep.objects.update_or_create(skill=skill, position=paso["position"], defaults=valores)
        posiciones.append(paso["position"])
    skill.steps.exclude(position__in=posiciones).delete()

    claves = []
    for param in data.get("parameters", []):
        valores = {f: param[f] for f in PARAMETER_FIELDS if f in param and f != "key"}
        SkillParameter.objects.update_or_create(skill=skill, key=param["key"], defaults=valores)
        claves.append(param["key"])
    skill.parameters.exclude(key__in=claves).delete()

    organization.enabled_skills.add(skill)
    if data.get("default_en_institucion"):
        organization.default_project_skills.add(skill)
    return skill


def cargar_todos(organization) -> dict[str, Skill]:
    return {slug: cargar(slug, organization) for slug in SLUGS}
