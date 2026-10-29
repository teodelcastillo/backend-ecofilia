"""
La configuración de cada institución, en datos (fase 1).

Hasta la fase 1, todo lo que hace distinto al portal de CAF estaba escrito en el
código, en el back y en el front:
- las etapas de una operación;
- qué workflow lanza cada una;
- cuál es el informe, cuál el guiado y cuál la lectura;
- la etiqueta del documento principal;
- los grupos de etiquetas;
- los criterios de evaluación.

Esto lo saca a ``Organization.config``, un documento versionado que tiene la
forma descrita en ``backend/docs/contrato-instituciones.md`` (fase 1).

- :func:`effective_config` es lo que usa el resto del código: la configuración
  de la institución con lo que le falte completado desde :data:`BASE_CONFIG`,
  o la base entera si no tiene (ni institución ni configuración propia).
- :func:`validate_config` es la puerta de escritura: devuelve el documento
  normalizado o la lista de errores, cada uno con la ruta del campo.

La configuración de CAF no vive acá: la siembra la migración
``user/0009_config_caf`` con los valores que tenía el código. Que no haya una
copia en el código es lo que impide que «CAF» vuelva a ser un caso especial.
"""
from __future__ import annotations

import copy
import re

CONFIG_VERSION = 1

TONES = ("ok", "atencion", "brecha", "critico", "pendiente", "neutro")

# Las reglas de determinación de un marco de evaluación están en código (ver
# ``apps.project.services.caf_criteria``) y dependen de sus claves: un marco
# conocido exige exactamente esos criterios.
FRAMEWORKS: dict[str, tuple[str, ...]] = {
    "caf-datbc": (
        "ct_a1", "ct_a2", "ct_a3", "objetivo_1",
        "ct_m1", "ct_m2", "ct_m3", "ct_m4", "ct_m5", "objetivo_2",
    ),
}

GROUP_OTRO = "otro"

COUNTRY_CONTEXT_TAGS = [
    "ndc",
    "nap",
    "lts",
    "comunicacion-adaptacion",
    "nbsap",
    "pancd",
    "inventario-gei",
    "ley-cambio-climatico",
    "reporte-nacional",
]

# Para las operaciones sin institución y las instituciones sin configuración
# propia. No tiene etapas ni workflows: nada de lo de una institución.
BASE_CONFIG: dict = {
    "version": CONFIG_VERSION,
    "branding": {"display_name": "Ecofilia", "logo_url": ""},
    "stages": [],
    "workflows": {"report": None, "guided": None, "reading": None},
    "evidence_tag_groups": [
        {"slug": "contexto", "label": "Contexto país", "tags": list(COUNTRY_CONTEXT_TAGS)},
    ],
    "evaluation": {"framework": "", "criteria": []},
}

TOP_LEVEL_KEYS = tuple(BASE_CONFIG)
WORKFLOW_ROLES = ("report", "guided", "reading")

_SLUG = re.compile(r"^[-a-z0-9_]+$")


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

def effective_config(organization) -> dict:
    """La configuración que rige para ``organization`` (``None``: la base).

    Nunca se modifica el resultado en el lugar: es una copia.
    """
    stored = getattr(organization, "config", None) if organization is not None else None
    config = copy.deepcopy(BASE_CONFIG)
    if isinstance(stored, dict) and stored:
        for key in TOP_LEVEL_KEYS:
            if key in stored:
                config[key] = copy.deepcopy(stored[key])
    elif organization is not None:
        config["branding"]["display_name"] = organization.name
    return config


def config_for_project(project) -> dict:
    organization = None
    if getattr(project, "organization_id", None):
        organization = project.organization
    return effective_config(organization)


def stage_slugs(config: dict) -> list[str]:
    return [stage["slug"] for stage in config.get("stages") or []]


def stage(config: dict, slug: str) -> dict | None:
    return next((s for s in config.get("stages") or [] if s.get("slug") == slug), None)


def stage_label(config: dict, slug: str) -> str:
    found = stage(config, slug)
    return found["label"] if found else slug


def workflow(config: dict, role: str) -> str | None:
    return (config.get("workflows") or {}).get(role) or None


def report_stages(config: dict) -> list[str]:
    """Las etapas con informe de Ecofilia: las que lanzan el workflow del informe."""
    report = workflow(config, "report")
    if not report:
        return []
    return [s["slug"] for s in config.get("stages") or [] if report in (s.get("launches") or [])]


def principal_tag(config: dict, slug: str) -> str | None:
    found = stage(config, slug)
    return (found or {}).get("principal_tag") or None


def principal_tags(config: dict) -> set[str]:
    return {s["principal_tag"] for s in config.get("stages") or [] if s.get("principal_tag")}


def tag_group(config: dict, tag_slug: str) -> str:
    for group in config.get("evidence_tag_groups") or []:
        if tag_slug in (group.get("tags") or []):
            return group["slug"]
    return GROUP_OTRO


def criteria(config: dict) -> list[dict]:
    return list((config.get("evaluation") or {}).get("criteria") or [])


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------

def validate_config(raw, organization) -> tuple[dict | None, list[str]]:
    """Valida y normaliza un documento de configuración para ``organization``.

    Devuelve ``(config, [])`` o ``(None, errores)``. Junta todos los errores en
    vez de cortar en el primero: quien edita la configuración en la consola los
    quiere ver todos.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["config: tiene que ser un objeto."]
    unknown = sorted(set(raw) - set(TOP_LEVEL_KEYS))
    if unknown:
        errors.append(f"config: claves desconocidas: {', '.join(unknown)}.")
    config = copy.deepcopy(BASE_CONFIG)
    config["branding"]["display_name"] = organization.name if organization is not None else "Ecofilia"
    for key in TOP_LEVEL_KEYS:
        if key in raw:
            config[key] = copy.deepcopy(raw[key])

    if config.get("version") != CONFIG_VERSION:
        errors.append(f"version: tiene que ser {CONFIG_VERSION}.")

    config["branding"] = _branding(config.get("branding"), errors)
    tags = _known_tags(organization)
    skills = _usable_skills(organization)
    config["stages"] = _stages(config.get("stages"), tags, skills, errors)
    config["workflows"] = _workflows(config.get("workflows"), skills, errors)
    config["evidence_tag_groups"] = _groups(config.get("evidence_tag_groups"), tags, errors)
    config["evaluation"] = _evaluation(config.get("evaluation"), errors)
    return (None, errors) if errors else (config, [])


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_slug(value) -> bool:
    return isinstance(value, str) and bool(_SLUG.match(value))


def _known_tags(organization) -> set[str]:
    from apps.document.services import evidence_tags_for_organization

    return set(
        evidence_tags_for_organization(getattr(organization, "pk", None)).values_list("slug", flat=True)
    )


def _usable_skills(organization) -> set[str]:
    """Las skills que la institución puede lanzar: públicas o suyas."""
    from django.db.models import Q

    from apps.skill.models import Skill, SkillScope

    usable = Q(scope=SkillScope.PUBLIC)
    if organization is not None:
        usable |= Q(scope=SkillScope.ORGANIZATION, organization=organization)
    return set(Skill.objects.filter(usable).values_list("slug", flat=True))


def _skill_error(path: str, slug, skills: set[str]) -> str | None:
    if not _is_slug(slug):
        return f"{path}: tiene que ser el slug de una skill."
    if slug not in skills:
        return f"{path}: la skill «{slug}» no existe, o no es pública ni de esta institución."
    return None


def _branding(value, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append("branding: tiene que ser un objeto.")
        return {"display_name": "", "logo_url": ""}
    unknown = sorted(set(value) - {"display_name", "logo_url"})
    if unknown:
        errors.append(f"branding: claves desconocidas: {', '.join(unknown)}.")
    display_name = _text(value.get("display_name"))
    if not display_name:
        errors.append("branding.display_name: no puede estar vacío.")
    logo_url = _text(value.get("logo_url"))
    if logo_url and not (logo_url.startswith("/") or logo_url.startswith("https://")):
        errors.append("branding.logo_url: tiene que ser una ruta del front (/...) o una URL https.")
    return {"display_name": display_name, "logo_url": logo_url}


def _stages(value, tags: set[str], skills: set[str], errors: list[str]) -> list[dict]:
    if not isinstance(value, list):
        errors.append("stages: tiene que ser una lista.")
        return []
    out, seen = [], set()
    for i, item in enumerate(value):
        path = f"stages[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: tiene que ser un objeto.")
            continue
        unknown = sorted(set(item) - {"slug", "label", "principal_tag", "launches"})
        if unknown:
            errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")
        slug = item.get("slug")
        if not _is_slug(slug):
            errors.append(f"{path}.slug: tiene que ser un slug (minúsculas, números, - o _).")
        elif slug in seen:
            errors.append(f"{path}.slug: «{slug}» está repetida.")
        seen.add(slug)
        label = _text(item.get("label"))
        if not label:
            errors.append(f"{path}.label: no puede estar vacía.")
        principal = item.get("principal_tag")
        if principal is not None and principal not in tags:
            errors.append(f"{path}.principal_tag: la etiqueta «{principal}» no existe para esta institución.")
        launches = item.get("launches", [])
        if not isinstance(launches, list):
            errors.append(f"{path}.launches: tiene que ser una lista.")
            launches = []
        for j, skill in enumerate(launches):
            error = _skill_error(f"{path}.launches[{j}]", skill, skills)
            if error:
                errors.append(error)
        out.append(
            {
                "slug": slug,
                "label": label,
                "principal_tag": principal or None,
                "launches": list(dict.fromkeys(launches)),
            }
        )
    return out


def _workflows(value, skills: set[str], errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append("workflows: tiene que ser un objeto.")
        return {role: None for role in WORKFLOW_ROLES}
    unknown = sorted(set(value) - set(WORKFLOW_ROLES))
    if unknown:
        errors.append(f"workflows: claves desconocidas: {', '.join(unknown)}.")
    out = {}
    for role in WORKFLOW_ROLES:
        slug = value.get(role)
        if slug is not None:
            error = _skill_error(f"workflows.{role}", slug, skills)
            if error:
                errors.append(error)
        out[role] = slug or None
    return out


def _groups(value, tags: set[str], errors: list[str]) -> list[dict]:
    if not isinstance(value, list):
        errors.append("evidence_tag_groups: tiene que ser una lista.")
        return []
    out, seen_groups, seen_tags = [], set(), {}
    for i, item in enumerate(value):
        path = f"evidence_tag_groups[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: tiene que ser un objeto.")
            continue
        unknown = sorted(set(item) - {"slug", "label", "tags"})
        if unknown:
            errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")
        slug = item.get("slug")
        if not _is_slug(slug):
            errors.append(f"{path}.slug: tiene que ser un slug.")
        elif slug == GROUP_OTRO:
            errors.append(f"{path}.slug: «{GROUP_OTRO}» está reservado para las etiquetas sin grupo.")
        elif slug in seen_groups:
            errors.append(f"{path}.slug: «{slug}» está repetido.")
        seen_groups.add(slug)
        label = _text(item.get("label"))
        if not label:
            errors.append(f"{path}.label: no puede estar vacía.")
        group_tags = item.get("tags", [])
        if not isinstance(group_tags, list):
            errors.append(f"{path}.tags: tiene que ser una lista.")
            group_tags = []
        for j, tag in enumerate(group_tags):
            tag_path = f"{path}.tags[{j}]"
            if tag not in tags:
                errors.append(f"{tag_path}: la etiqueta «{tag}» no existe para esta institución.")
            elif tag in seen_tags and seen_tags[tag] != slug:
                errors.append(f"{tag_path}: «{tag}» ya está en el grupo «{seen_tags[tag]}».")
            seen_tags.setdefault(tag, slug)
        out.append({"slug": slug, "label": label, "tags": list(dict.fromkeys(group_tags))})
    return out


def _evaluation(value, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append("evaluation: tiene que ser un objeto.")
        return {"framework": "", "criteria": []}
    unknown = sorted(set(value) - {"framework", "criteria"})
    if unknown:
        errors.append(f"evaluation: claves desconocidas: {', '.join(unknown)}.")
    framework = value.get("framework") or ""
    if framework and framework not in FRAMEWORKS:
        errors.append(
            f"evaluation.framework: «{framework}» no existe. Conocidos: {', '.join(sorted(FRAMEWORKS))}."
        )
    raw_criteria = value.get("criteria", [])
    if not isinstance(raw_criteria, list):
        errors.append("evaluation.criteria: tiene que ser una lista.")
        raw_criteria = []
    out, keys = [], []
    for i, item in enumerate(raw_criteria):
        path = f"evaluation.criteria[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: tiene que ser un objeto.")
            continue
        unknown = sorted(set(item) - {"key", "code", "name", "group", "is_determination", "options"})
        if unknown:
            errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")
        key = item.get("key")
        if not _is_slug(key):
            errors.append(f"{path}.key: tiene que ser un slug.")
        elif key in keys:
            errors.append(f"{path}.key: «{key}» está repetida.")
        keys.append(key)
        code, name = _text(item.get("code")), _text(item.get("name"))
        if not code:
            errors.append(f"{path}.code: no puede estar vacío.")
        if not name:
            errors.append(f"{path}.name: no puede estar vacío.")
        options = item.get("options", [])
        if not isinstance(options, list) or not options:
            errors.append(f"{path}.options: tiene que ser una lista con al menos una opción.")
            options = []
        clean_options = []
        for j, option in enumerate(options):
            option_path = f"{path}.options[{j}]"
            if not isinstance(option, dict):
                errors.append(f"{option_path}: tiene que ser un objeto.")
                continue
            option_value = _text(option.get("value"))
            if not option_value:
                errors.append(f"{option_path}.value: no puede estar vacío.")
            tone = option.get("tone")
            if tone not in TONES:
                errors.append(f"{option_path}.tone: tiene que ser uno de {', '.join(TONES)}.")
            clean_options.append(
                {"value": option_value, "short": _text(option.get("short")) or option_value, "tone": tone}
            )
        out.append(
            {
                "key": key,
                "code": code,
                "name": name,
                "group": item.get("group") or None,
                "is_determination": bool(item.get("is_determination")),
                "options": clean_options,
            }
        )
    valid_keys = {c["key"] for c in out}
    for i, criterion in enumerate(out):
        if criterion["group"] is not None and criterion["group"] not in valid_keys:
            errors.append(f"evaluation.criteria[{i}].group: «{criterion['group']}» no es un criterio.")
    if framework in FRAMEWORKS and tuple(keys) != FRAMEWORKS[framework]:
        errors.append(
            f"evaluation.criteria: el marco «{framework}» exige exactamente estos criterios, "
            f"en este orden: {', '.join(FRAMEWORKS[framework])}."
        )
    return {"framework": framework, "criteria": out}
