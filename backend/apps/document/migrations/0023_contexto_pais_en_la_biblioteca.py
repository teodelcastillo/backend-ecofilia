"""
Los instrumentos de contexto país que ya estaban en la biblioteca se etiquetan.

Las etiquetas ``ley-cambio-climatico`` y ``reporte-nacional`` (0021) sólo se
proponían a documentos nuevos subidos sin etiqueta: la ley de cambio climático,
el BTR, el BUR o la Comunicación Nacional que ya estaban cargados no las
tenían, y la sincronización por país (``country_documents``) no los vinculaba.

- Se amplían los temas que las proponen (formas en plural y variantes).
- A cada documento con país que no la tenga se le suma por sus temas o por su
  nombre. No se quita ninguna etiqueta.
- A los documentos con país que no tenían ninguna etiqueta se les proponen,
  por sus temas, las demás etiquetas del contexto país (NDC, NAP, LTS, AdCom,
  NBSAP), igual que al subirlos.
"""
import re
import unicodedata

from django.db import migrations

TEMAS = {
    "ley-cambio-climatico": [
        "ley de cambio climatico",
        "ley marco de cambio climatico",
        "ley marco sobre cambio climatico",
        "leyes de cambio climatico",
        "ley nacional de cambio climatico",
    ],
    "reporte-nacional": [
        "btr",
        "bur",
        "comunicacion nacional",
        "comunicaciones nacionales",
        "informe bienal de transparencia",
        "informe bienal de actualizacion",
        "informes bienales",
    ],
}

# Por nombre: sólo las dos etiquetas nuevas, con patrones estrechos.
NOMBRE = {
    "ley-cambio-climatico": re.compile(r"\bley\b.*\bcambio climatico\b"),
    "reporte-nacional": re.compile(
        r"\b(btr|bur)\b|informe bienal|comunicaci(on|ones) nacional(es)?\b|national communication|biennial"
    ),
}

CONTEXTO = ["ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap", "ley-cambio-climatico", "reporte-nacional"]

_SPLIT = re.compile(r"[,;/()_\-\s]+")


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value or "")
    return " ".join("".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower().split())


def _keys(topics) -> set[str]:
    keys: set[str] = set()
    for raw in topics or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = _norm(raw)
        keys.add(value)
        head, sep, _ = value.partition(":")
        if sep and head.strip():
            keys.add(head.strip())
        keys.update(frag for frag in _SPLIT.split(value) if len(frag) >= 3)
    return keys


def _match(source_topics, keys: set[str]) -> bool:
    for raw in source_topics or []:
        wanted = _norm(raw)
        if not wanted:
            continue
        if wanted in keys:
            return True
        if " " in wanted and any(re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", k) for k in keys):
            return True
    return False


def aplicar(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Document = apps.get_model("document", "Document")

    for slug, extra in TEMAS.items():
        tag = EvidenceTag.objects.filter(slug=slug).first()
        if tag is None:
            continue
        tag.source_topics = sorted(set(tag.source_topics or []) | set(extra))
        tag.save(update_fields=["source_topics"])

    tags = {t.slug: t for t in EvidenceTag.objects.filter(slug__in=CONTEXTO)}
    for doc in Document.objects.filter(region__isnull=False).exclude(region="").prefetch_related("evidence_tags"):
        actuales = {t.slug for t in doc.evidence_tags.all()}
        keys = _keys(doc.topics)
        nombre = _norm(doc.name)
        sumar = []
        for slug, tag in tags.items():
            if slug in actuales:
                continue
            nuevas = slug in NOMBRE
            if nuevas and (_match(tag.source_topics, keys) or NOMBRE[slug].search(nombre)):
                sumar.append(tag)
            elif not nuevas and not actuales and _match(tag.source_topics, keys):
                sumar.append(tag)
        if sumar:
            doc.evidence_tags.add(*sumar)


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0022_seed_otros_tag"),
    ]

    operations = [
        migrations.RunPython(aplicar, migrations.RunPython.noop),
    ]
