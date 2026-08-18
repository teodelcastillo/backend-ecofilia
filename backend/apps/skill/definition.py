"""
La definición de un workflow, serializada: qué se ejecutó, exactamente.

Hoy la definición vive editable en la base. Alguien corrige la instrucción de un
paso, se vuelve a correr, sale distinto, y no queda registro de que lo que
cambió fue la definición y no el modelo. El manifiesto ya guardaba una huella
para detectarlo, pero una huella sola dice *que* algo cambió, no *qué*: sirve
para desconfiar, no para explicar.

Este módulo produce el objeto que sí lo explica — la definición entera,
serializada — y de ahí derivan las tres cosas que la Fase 7 necesita:

* la **huella**, que es su sha256;
* la **versión**, que es esa serialización guardada como fila inmutable y a la
  que cada corrida queda apuntando;
* el **diff**, que dice campo por campo qué se movió entre dos versiones.

Es agnóstico del workflow a propósito. No sabe qué es un IET: recorre las listas
de campos declaradas más abajo. Agregar un campo al modelo es agregar su nombre
a una tupla, y si no se agrega a ninguna, ``test_definition.py`` falla — así el
próximo campo que afecte la salida no puede entrar sin que alguien decida si es
parte de la definición o no.

No importa modelos de Django: trabaja por ``getattr`` sobre cualquier objeto con
la forma correcta. Eso lo hace testeable sin base y sin fixtures, igual que
``context_budget``.
"""
from __future__ import annotations

import hashlib
import json

# Cambiar la *forma* de la serialización mueve todas las huellas de golpe. Con
# el esquema versionado, una comparación puede distinguir "cambió la definición"
# de "cambió cómo la serializamos", que es la diferencia entre un cambio real y
# un deploy nuestro.
DEFINITION_SCHEMA = 1

# Un `skill_ref` apunta a otra skill, y esa otra skill también puede cambiar. Se
# le calcula huella propia hasta esta profundidad; más abajo se registra sólo el
# slug. Uno alcanza para el caso real —un paso que delega en una skill rápida—
# sin abrir la puerta a recursión infinita entre definiciones que se referencien.
MAX_LINK_DEPTH = 1


# ---------------------------------------------------------------------------
# Qué es parte de la definición
# ---------------------------------------------------------------------------

# Todo lo que puede cambiar la salida. `name` y `description` están acá porque
# no son decorativos: cuando no hay `retrieval_query_template`, la consulta de
# recuperación se arma con ellos.
SKILL_FIELDS = (
    "skill_type",
    "name",
    "description",
    "system_prompt",
    "prompt_template",
    "tier",
    "model",
    "temperature",
    "comparative_mode_enabled",
    "strict_missing_evidence",
    "retrieval_query_template",
    "retrieval_strategy",
    "k_per_doc",
    "total_limit",
    "max_per_doc_after_rerank",
    "default_output_mode",
    "table_schema",
    "pinned_document_slugs",
    "tools_enabled",
    "research_phase_enabled",
    "research_queries",
)

# Lo que existe en el modelo y deliberadamente no es parte de la definición.
# Está enumerado en vez de omitido para que el test de cobertura pueda exigir
# que cada campo esté en una lista o en la otra.
SKILL_FIELDS_IGNORED = (
    "id",
    "owner",
    "slug",              # identidad de la skill, no contenido de la definición
    "allowed_contexts",  # dónde se la puede correr, no qué produce
    "is_template",
    "is_default_enabled",
    "created_at",
    "updated_at",
)

STEP_FIELDS = (
    "position",
    "title",
    "instructions",
    "step_type",
    "document_slugs",
    "tier",
    "evidence_mode",
    "output_mode",
    "table_schema",
    "output_validation",
    "approval_required",
)

STEP_FIELDS_IGNORED = (
    "id",
    "skill",
    # Se serializa aparte, con la huella de la skill referenciada: guardar sólo
    # el id dejaría pasar un cambio en la skill que el paso ejecuta.
    "linked_skill",
)

PARAMETER_FIELDS = (
    "key",
    "label",
    "param_type",
    "description",
    "default_value",
    "required",
    "options",
    "position",
)

PARAMETER_FIELDS_IGNORED = ("id", "skill")


# ---------------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------------

def _values(obj, fields) -> dict:
    out = {}
    for name in fields:
        value = getattr(obj, name, None)
        if isinstance(value, (list, dict)):
            # Copia: la serialización no puede quedar compartiendo estructura
            # con el objeto vivo, o una mutación posterior reescribiría el
            # snapshot que se guardó como inmutable.
            value = json.loads(json.dumps(value, default=str))
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            value = str(value)
        out[name] = value
    return out


def _linked_reference(step, depth: int) -> dict | None:
    """La skill que un paso ``skill_ref`` ejecuta, con huella propia."""
    linked = getattr(step, "linked_skill", None)
    if linked is None:
        return None
    reference = {"slug": getattr(linked, "slug", "")}
    if depth >= MAX_LINK_DEPTH:
        # Se registra la identidad y se declara que no se miró más adentro, en
        # vez de dejar creer que la huella cubre también esta rama.
        reference["fingerprint"] = None
        reference["truncated"] = True
        return reference
    reference["fingerprint"] = fingerprint(
        serialize_definition(linked, _depth=depth + 1)
    )
    return reference


def serialize_definition(skill, *, steps=None, parameters=None, _depth: int = 0) -> dict:
    """
    La definición completa de ``skill``, en un dict estable y serializable.

    ``steps`` y ``parameters`` se pueden pasar ya cargados —el runner los tiene
    a mano y así no se repite la consulta—; si no, se leen de la relación.
    """
    if steps is None:
        related = getattr(skill, "steps", None)
        steps = list(related.all()) if related is not None else []
    if parameters is None:
        related = getattr(skill, "parameters", None)
        parameters = list(related.all()) if related is not None else []

    serialized_steps = []
    for step in sorted(steps, key=lambda s: getattr(s, "position", 0)):
        entry = _values(step, STEP_FIELDS)
        entry["linked_skill"] = _linked_reference(step, _depth)
        serialized_steps.append(entry)

    return {
        "schema": DEFINITION_SCHEMA,
        "skill": _values(skill, SKILL_FIELDS),
        "steps": serialized_steps,
        "parameters": [
            _values(p, PARAMETER_FIELDS)
            for p in sorted(parameters, key=lambda p: (getattr(p, "position", 0), getattr(p, "key", "")))
        ],
    }


def fingerprint(definition: dict) -> str:
    blob = json.dumps(definition, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Versionado
# ---------------------------------------------------------------------------

def resolve_definition_version(skill, *, steps=None, parameters=None):
    """
    La versión que corresponde a la definición actual de ``skill``, creándola si
    es nueva.

    La versión se **deriva**, no se declara: nadie tiene que acordarse de
    publicar. Editás un paso, corrés, y la corrida queda apuntando a una versión
    nueva. Como la identidad es la huella, revertir una edición devuelve la
    versión anterior en vez de crear una tercera — que es lo correcto: dos
    corridas con definiciones idénticas *son* comparables, sin importar qué pasó
    en el medio.
    """
    from django.db import IntegrityError, transaction

    from apps.skill.models import SkillDefinitionVersion

    definition = serialize_definition(skill, steps=steps, parameters=parameters)
    huella = fingerprint(definition)

    for _ in range(3):
        existing = SkillDefinitionVersion.objects.filter(
            skill=skill, fingerprint=huella
        ).first()
        if existing is not None:
            return existing
        last = (
            SkillDefinitionVersion.objects.filter(skill=skill)
            .order_by("-version_number")
            .first()
        )
        try:
            with transaction.atomic():
                return SkillDefinitionVersion.objects.create(
                    skill=skill,
                    version_number=(last.version_number + 1) if last else 1,
                    fingerprint=huella,
                    schema=DEFINITION_SCHEMA,
                    definition=definition,
                )
        except IntegrityError:
            # Dos corridas de la misma skill arrancando a la vez: una ganó. El
            # ciclo vuelve a mirar, y ahora encuentra su fila o el número
            # siguiente libre.
            continue
    raise RuntimeError(
        f"No se pudo resolver la versión de definición de la skill {skill.pk}."
    )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

MAX_DIFF_VALUE_CHARS = 300


def _short(value) -> object:
    if isinstance(value, str) and len(value) > MAX_DIFF_VALUE_CHARS:
        return value[:MAX_DIFF_VALUE_CHARS] + f"… (+{len(value) - MAX_DIFF_VALUE_CHARS} car.)"
    return value


def _compare_mapping(a: dict, b: dict, *, prefix: str, out: list) -> None:
    for key in sorted(set(a) | set(b)):
        if key not in a:
            out.append({"path": f"{prefix}{key}", "kind": "added", "b": _short(b[key])})
        elif key not in b:
            out.append({"path": f"{prefix}{key}", "kind": "removed", "a": _short(a[key])})
        elif a[key] != b[key]:
            out.append({
                "path": f"{prefix}{key}",
                "kind": "changed",
                "a": _short(a[key]),
                "b": _short(b[key]),
            })


def _compare_collection(a: list, b: list, *, key: str, prefix: str, out: list) -> None:
    by_key_a = {item.get(key): item for item in a}
    by_key_b = {item.get(key): item for item in b}
    for item_key in sorted(set(by_key_a) | set(by_key_b), key=lambda k: (k is None, k)):
        label = f"{prefix}[{item_key}]"
        if item_key not in by_key_a:
            out.append({"path": label, "kind": "added"})
        elif item_key not in by_key_b:
            out.append({"path": label, "kind": "removed"})
        else:
            _compare_mapping(
                by_key_a[item_key], by_key_b[item_key], prefix=f"{label}.", out=out
            )


def diff_definitions(a: dict, b: dict) -> list[dict]:
    """
    Qué se movió entre dos definiciones, campo por campo.

    Los pasos se emparejan por posición y los parámetros por clave, no por orden
    de lista: insertar un paso al principio no debe leerse como "cambiaron los
    diecisiete".
    """
    out: list[dict] = []
    if a.get("schema") != b.get("schema"):
        out.append({
            "path": "schema",
            "kind": "changed",
            "a": a.get("schema"),
            "b": b.get("schema"),
            "note": (
                "cambió la forma de la serialización, no necesariamente la "
                "definición: las huellas de un esquema no son comparables con "
                "las del otro."
            ),
        })
    _compare_mapping(a.get("skill") or {}, b.get("skill") or {}, prefix="skill.", out=out)
    _compare_collection(
        a.get("steps") or [], b.get("steps") or [], key="position", prefix="steps", out=out
    )
    _compare_collection(
        a.get("parameters") or [],
        b.get("parameters") or [],
        key="key",
        prefix="parameters",
        out=out,
    )
    return out
