"""
Una operación de prueba por etapa de CAF (IDO y DEC), con una corrida completada
del informe de Ecofilia de esa etapa.

Todo es inventado: nombres, montos, documentos y texto. Lo que sí copia de
producción es la **estructura** del informe, porque es lo que el portal lee sin
IA (``ecofiliaV2/lib/caf-informe-lectura.ts`` y ``caf-insumo.ts``,
``apps/project/services/caf_criteria.py``):

- la sección de co-beneficios (C.1 en IDO, B.1 en DEC) con una tabla
  Campo | Detalle por actividad, con las variantes de rótulo de la fila de la
  actividad que se ven en producción;
- cada CT con su prosa y la línea de cierre ``**Conclusión:**`` /
  ``**Información faltante:**``;
- las recomendaciones (E.3 en IDO, D.1 en DEC) con subtítulos numerados y
  viñetas;
- S.1 (síntesis por criterio) y S.2 (componentes verdes) como tablas, cuando el
  workflow los tiene (la IET de DEC no).

Las secciones se reconocen por lo que dice el título, igual que en el portal: el
contenido sigue a los workflows aunque el staff les cambie la letra.
"""
from __future__ import annotations

import re

from django.utils import timezone

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument, ProjectStageTransition
from apps.skill.models import ExecutionStatus, SkillExecution

SEED_MARK = "seed_local_instituciones"

_ACTIVIDADES_IDO = [
    {
        "titulo": "1. Reducción de pérdidas en la red de agua potable",
        "componente": "Componente 1. Rehabilitación de redes y sectorización hidráulica en la ciudad de Valle Azul",
        "monto": "12.400.000",
        "categoria": "A.1 Gestión y Suministro de Agua y Saneamiento",
        "subcategoria": "A.1.1 Medidas que contribuyen a mejorar el acceso y/o el uso eficiente del agua",
        "rotulo": "Actividad elegible (nivel 4)",
        "codigo": "A.1.1.6",
        "actividad": "Medidas para el control activo de fugas de agua",
        "objetivo": "Adaptación",
        "justificacion": (
            "El documento de la operación prevé sectorizar la red y reemplazar 45 km de tuberías con "
            "pérdidas superiores al 40 %, lo que reduce la presión sobre fuentes expuestas a sequías más largas."
        ),
    },
    {
        "titulo": "2. Eficiencia energética en estaciones de bombeo",
        "componente": "Componente 2. Modernización de cuatro estaciones de bombeo con variadores de velocidad",
        "monto": "3.100.000",
        "categoria": "M.8 Eficiencia energética de uso final en edificaciones o equipos",
        "subcategoria": (
            "M.8.1 Eficiencia energética, energías renovables, reducción de emisiones de CO2e "
            "y sumideros de carbono en edificios y espacios públicos"
        ),
        "rotulo": "Actividad",
        "codigo": "M.8.1.1",
        "actividad": "Medidas que reduzcan el consumo neto de energía",
        "objetivo": "Mitigación",
        "justificacion": (
            "Los variadores y bombas de alta eficiencia reducen un 25 % el consumo eléctrico estimado "
            "de las estaciones, según la memoria técnica preliminar."
        ),
    },
    {
        "titulo": "3. Restauración de la cuenca de abastecimiento",
        "componente": "Componente 3. Restauración de 600 ha de bofedales en la cabecera de la cuenca",
        "monto": "No disponible desglosado en el documento de la operación",
        "categoria": "B.2 Protección y restauración de la biodiversidad y los ecosistemas",
        "subcategoria": "B.2.1 Conservación y restauración de ecosistemas estratégicos terrestres",
        "rotulo": "Actividad de financiamiento elegible",
        "codigo": "B.2.1.6",
        "actividad": "Restauración de áreas degradadas (suelos degradados)",
        "objetivo": "Biodiversidad y otros objetivos ambientales",
        "justificacion": (
            "La restauración de bofedales mejora la regulación hídrica de la cuenca y protege hábitats "
            "de altura; el monto no está separado del componente de gestión de cuencas."
        ),
    },
]

_ACTIVIDADES_DEC = [
    {
        "titulo": "1. Generación solar para el sistema de bombeo",
        "componente": "Componente 1. Parque solar de 8 MW para abastecer el sistema de riego de Río Claro",
        "monto": "9.800.000",
        "categoria": "M.1 Energía",
        "subcategoria": "M.1.1 Generación de energía renovable",
        "rotulo": "Actividad de financiamiento elegible",
        "codigo": "M.1.1.1",
        "actividad": "Generación de energía renovable con bajas emisiones de GEI",
        "objetivo": "Mitigación",
        "justificacion": (
            "La Misión de Evaluación confirmó el diseño del parque solar y su conexión exclusiva al "
            "sistema de bombeo, que hoy funciona con generadores diésel."
        ),
    },
    {
        "titulo": "2. Riego tecnificado con captación de agua de lluvia",
        "componente": "Componente 2. Reservorios y riego por goteo para 1.200 productores",
        "monto": "14.500.000",
        "categoria": "A.1 Gestión y Suministro de Agua y Saneamiento",
        "subcategoria": "A.1.1 Medidas que contribuyen a mejorar el acceso y/o el uso eficiente del agua",
        "rotulo": "Actividad elegible (nivel 4)",
        "codigo": "A.1.1.2",
        "actividad": "Instalación de equipos de captura y almacenamiento de aguas",
        "objetivo": "Adaptación",
        "justificacion": (
            "Los reservorios amortiguan la variabilidad de lluvias identificada en el análisis de "
            "riesgos; el diseño final incorpora el caudal de diseño con escenarios de cambio climático."
        ),
    },
    {
        "titulo": "3. Reforestación de laderas",
        "componente": "Componente 3. Reforestación con especies nativas en 350 ha de laderas erosionadas",
        "monto": "1.700.000",
        "categoria": "B.2 Protección y restauración de la biodiversidad y los ecosistemas",
        "subcategoria": "B.2.1 Conservación y restauración de ecosistemas estratégicos terrestres",
        "rotulo": "Actividad",
        "codigo": "B.2.1.8",
        "actividad": "Inversión en cobertura vegetal para el control de erosión",
        "objetivo": "Biodiversidad y otros objetivos ambientales",
        "justificacion": "Controla la erosión que hoy colmata los canales y recupera corredores de especies nativas.",
    },
]

ESCENARIOS = {
    "ido_cci": {
        "slug": "local-caf-ido",
        "name": "Agua y energía resiliente en Valle Azul (prueba IDO)",
        "doc_slug": "doc-local-caf-ido",
        "doc_name": "IDO - Agua y energía resiliente en Valle Azul (inventado).pdf",
        "principal_tag": "ido",
        "notes": {
            "pais": "Perú", "year": 2026, "sector": "Agua y saneamiento",
            "client": "Empresa Municipal de Agua de Valle Azul (ficticia)",
            "numero_operacion": "LOCAL-IDO-001", "monto": "25",
            "objetivo": "Mejorar la continuidad y la eficiencia del servicio de agua potable de Valle Azul.",
            "componentes": "1. Rehabilitación de redes. 2. Estaciones de bombeo. 3. Restauración de la cuenca.",
        },
        "ct": {
            "CT A1": ("Riesgo medio", "El estudio de inundaciones del sitio de la planta de tratamiento."),
            "CT A2": ("No suficientes", "El diseño de las obras de protección frente a crecidas."),
            "CT A3": ("Consistencia", "Nada relevante."),
            "CT M1": ("Sin actividades no alineadas", "Nada relevante."),
            "CT M2": ("Contiene actividades alineadas", "Nada relevante."),
            "CT M3": ("Consistencia", "Nada relevante."),
            "CT M4": ("Potencialmente inconsistente", "La matriz energética futura de la empresa operadora."),
            "CT M5": ("No persisten riesgos de transición", "Nada relevante."),
        },
        "objetivos": {1: "Con brechas por CT A2", 2: "Alineada en el CT M2"},
        "actividades": _ACTIVIDADES_IDO,
    },
    "dec": {
        "slug": "local-caf-dec",
        "name": "Riego y energía solar en Río Claro (prueba DEC)",
        "doc_slug": "doc-local-caf-dec",
        "doc_name": "DEC - Riego y energía solar en Río Claro (inventado).pdf",
        "principal_tag": "dec",
        "previous_doc": ("doc-local-caf-dec-ido", "IDO - Riego y energía solar en Río Claro (inventado).pdf", "ido"),
        "notes": {
            "pais": "Colombia", "year": 2026, "sector": "Agricultura y riego",
            "client": "Distrito de Riego de Río Claro (ficticio)",
            "numero_operacion": "LOCAL-DEC-002", "monto": "30",
            "objetivo": "Asegurar el riego de pequeños productores con energía renovable y agua de lluvia.",
            "componentes": "1. Parque solar. 2. Reservorios y riego por goteo. 3. Reforestación de laderas.",
        },
        "ct": {
            "CT A1": ("Riesgo bajo", "Nada relevante."),
            "CT A2": ("No aplica", "Nada relevante."),
            "CT A3": ("Consistencia", "Nada relevante."),
            "CT M1": ("Sin actividades no alineadas", "Nada relevante."),
            "CT M2": ("Sin actividades alineadas", "Nada relevante."),
            "CT M3": ("Potencialmente inconsistente", "La meta sectorial de riego de la NDC actualizada."),
            "CT M4": ("Consistencia", "Nada relevante."),
            "CT M5": ("Persisten riesgos de transición", "El plan de retiro de los generadores diésel."),
        },
        "objetivos": {1: "Alineada", 2: "Con brechas"},
        "actividades": _ACTIVIDADES_DEC,
    },
}

_PROSA_CT = {
    "CT A1": (
        "El análisis de riesgos climáticos identifica amenazas de sequía prolongada y lluvias intensas "
        "sobre la infraestructura de la operación. La vulnerabilidad se concentra en las obras ubicadas "
        "cerca de cauces."
    ),
    "CT A2": (
        "Las medidas de adaptación previstas incluyen redundancia en el abastecimiento y diseño con "
        "márgenes para caudales extremos. Su suficiencia se evalúa frente a los riesgos de CT A1."
    ),
    "CT A3": (
        "La operación es coherente con las prioridades del Plan Nacional de Adaptación para los sectores "
        "de agua y agricultura, y con la comunicación de adaptación del país."
    ),
    "CT M1": (
        "Ninguna de las actividades financiadas figura en la lista de actividades consideradas no "
        "alineadas con el objetivo de desarrollo bajo en emisiones."
    ),
    "CT M2": (
        "Se revisaron las actividades frente a la lista de actividades alineadas de manera directa con "
        "el desarrollo bajo en emisiones de GEI."
    ),
    "CT M3": (
        "La NDC del país prioriza la eficiencia en el uso del agua y la energía renovable. Las metas "
        "sectoriales se contrastaron con el alcance de la operación."
    ),
    "CT M4": (
        "Las trayectorias de largo plazo del país prevén descarbonizar la generación eléctrica antes de "
        "2050; se analizó si la operación compromete esa trayectoria."
    ),
    "CT M5": (
        "Se analizaron riesgos de transición regulatorios, de mercado y tecnológicos asociados a la "
        "operación y a su demanda energética."
    ),
}

_CODIGO_PASO = re.compile(r"^\s*([A-Z](?:\.\d+)+)\.?\s")
_CT_EN_TITULO = re.compile(r"\bCT\s*([AM])\s*(\d)\b", re.I)
_RECOMENDACIONES = re.compile(r"^\s*[A-Z](\.\d+)*\.?\s+Recomendaciones\b", re.I)
_ACTIVIDADES_VERDES = re.compile(r"co-?beneficios|financiamiento verde|actividades elegibles", re.I)


def _codigo(titulo: str) -> str:
    m = _CODIGO_PASO.match(titulo)
    return m.group(1) if m else ""


def _ct(titulo: str) -> str | None:
    m = _CT_EN_TITULO.search(titulo)
    return f"CT {m.group(1).upper()}{m.group(2)}" if m else None


def _objetivo_de_determinacion(titulo: str) -> int | None:
    if not re.search(r"determinaci[oó]n", titulo, re.I) or _ct(titulo):
        return None
    m = re.search(r"Objetivo\s*([12])\b", titulo, re.I)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Contenido de cada sección
# ---------------------------------------------------------------------------

def _actividades_md(escenario: dict) -> str:
    partes = [
        "La operación presenta las siguientes actividades con potencial de co-beneficios ambientales, "
        "climáticos y de biodiversidad, clasificadas según el catálogo de actividades elegibles de CAF."
    ]
    for a in escenario["actividades"]:
        partes.append(
            f"### {a['titulo']}\n\n"
            "| Campo | Detalle |\n|---|---|\n"
            f"| **Componente y actividad de la operación** | {a['componente']} |\n"
            f"| **Monto (USD)** | {a['monto']} |\n"
            f"| **Categoría (nivel 2)** | {a['categoria']} |\n"
            f"| **Subcategoría (nivel 3)** | {a['subcategoria']} |\n"
            f"| **{a['rotulo']}** | {a['codigo']} {a['actividad']} |\n"
            f"| **Justificación y evidencia** | {a['justificacion']} |"
        )
    partes.append(
        "En conjunto, las actividades contribuyen a la adaptación del servicio frente a la variabilidad "
        "climática y a la reducción de emisiones por menor consumo de energía fósil."
    )
    return "\n\n".join(partes)


def _ct_md(escenario: dict, ct: str) -> str:
    valor, faltante = escenario["ct"][ct]
    prosa = _PROSA_CT[ct]
    return (
        f"{prosa}\n\n"
        "Los documentos de la operación aportan evidencia parcial sobre este criterio; la evaluación "
        "se apoya además en los instrumentos nacionales del país.\n\n"
        f"**Conclusión:** {valor}\n"
        f"**Información faltante:** {faltante}"
    )


def _determinacion_md(escenario: dict, objetivo: int) -> str:
    valor = escenario["objetivos"][objetivo]
    criterios = "CT A1 a CT A3" if objetivo == 1 else "CT M1 a CT M5"
    return (
        f"A partir de lo analizado en los criterios {criterios}, la operación se presenta como "
        f"«{valor}» respecto del Objetivo {objetivo}. La determinación final la toma el ejecutivo con la "
        "evidencia disponible."
    )


def _recomendaciones_md(codigo: str) -> str:
    base = codigo or "E.3"
    grupos = [
        ("Recomendaciones para fortalecer la alineación con el Objetivo 1: Desarrollo resiliente al clima", [
            "Completar el análisis de riesgo de inundación de las obras ubicadas en zonas bajas.",
            "Incorporar en el diseño márgenes de seguridad para caudales con escenarios de cambio climático.",
        ]),
        ("Recomendaciones para fortalecer la alineación con el Objetivo 2: Desarrollo bajo en emisiones de GEI", [
            "Documentar la matriz energética prevista para la operación durante su vida útil.",
            "Evaluar contratos de suministro con energía renovable para el consumo eléctrico remanente.",
        ]),
        ("Recomendaciones para fortalecer los co-beneficios ambientales, climáticos y de biodiversidad", [
            "Desagregar el monto de las actividades de restauración para medir su aporte verde.",
            "Definir indicadores de superficie restaurada y de caudal regulado en la matriz de resultados.",
        ]),
        ("Estudios complementarios sugeridos", [
            "Estudio hidrológico actualizado de la cuenca de abastecimiento.",
        ]),
    ]
    partes = []
    for i, (titulo, items) in enumerate(grupos, start=1):
        partes.append(f"### {base}.{i}. {titulo}\n\n" + "\n".join(f"- {item}" for item in items))
    return "\n\n".join(partes)


def _concepto_md(escenario: dict) -> str:
    return (
        "La DATBC considera que la operación avanzó respecto del IDO: la Misión de Evaluación confirmó el "
        "diseño de los componentes y los riesgos identificados cuentan con medidas. "
        f"Objetivo 1: {escenario['objetivos'][1]}. Objetivo 2: {escenario['objetivos'][2]}."
    )


def _generico_md(titulo: str, escenario: dict) -> str:
    return (
        f"Sección de prueba («{titulo}») de la operación «{escenario['name']}». El contenido es inventado "
        "para la base local: describe el contexto del país, las políticas de clima y biodiversidad "
        "relevantes y los antecedentes de la operación, sin datos de ningún cliente real."
    )


def _columnas(step, por_defecto: list[str]) -> list[str]:
    schema = step.table_schema or {}
    keys = [c.get("key") for c in schema.get("columns", []) if isinstance(c, dict) and c.get("key")]
    return keys or por_defecto


def _tabla(step, filas: list[dict], por_defecto: list[str]) -> dict:
    columnas = _columnas(step, por_defecto)
    return {
        "type": "table",
        "columns": columnas,
        "column_schema": (step.table_schema or {}).get("columns", []),
        "rows": [{c: fila.get(c, "") for c in columnas} for fila in filas],
    }


def _s1(step, escenario: dict) -> dict:
    filas = []
    orden = ["CT A1", "CT A2", "CT A3", 1, "CT M1", "CT M2", "CT M3", "CT M4", "CT M5", 2]
    for item in orden:
        if isinstance(item, int):
            filas.append({
                "criterio": f"Objetivo {item}",
                "determinacion": escenario["objetivos"][item],
                "fundamento": "Resulta de aplicar las reglas de la DATBC a los CT del objetivo.",
                "informacion_faltante": "Nada relevante.",
            })
        else:
            valor, faltante = escenario["ct"][item]
            filas.append({
                "criterio": item,
                "determinacion": valor,
                "fundamento": _PROSA_CT[item],
                "informacion_faltante": faltante,
            })
    return _tabla(step, filas, ["criterio", "determinacion", "fundamento", "informacion_faltante"])


def _monto_num(texto: str):
    digitos = texto.replace(".", "")
    return int(digitos) if digitos.isdigit() else None


def _s2(step, escenario: dict) -> dict:
    filas = [
        {
            "componente": a["componente"],
            "actividad_elegible": a["actividad"],
            "codigo_actividad": a["codigo"],
            "objetivo": a["objetivo"],
            "categoria": a["categoria"],
            "monto_usd": _monto_num(a["monto"]),
            "fundamento": a["justificacion"],
        }
        for a in escenario["actividades"]
    ]
    return _tabla(step, filas, list(filas[0]))


def _entrada(step, escenario: dict) -> dict:
    titulo = step.title
    entry = {
        "step_id": step.id,
        "title": titulo,
        "output_mode": "text",
        "model": "seed-local",
        "citations": [],
        "sources": [],
    }
    if re.match(r"^\s*S\.1\b", titulo):
        entry.update(output_mode="table", table=_s1(step, escenario), content="")
    elif re.match(r"^\s*S\.2\b", titulo):
        entry.update(output_mode="table", table=_s2(step, escenario), content="")
    elif re.match(r"^\s*S\.\d", titulo):
        entry["content"] = _generico_md(titulo, escenario)
    elif _ct(titulo) and _ct(titulo) in escenario["ct"]:
        entry["content"] = _ct_md(escenario, _ct(titulo))
    elif _objetivo_de_determinacion(titulo):
        entry["content"] = _determinacion_md(escenario, _objetivo_de_determinacion(titulo))
    elif _RECOMENDACIONES.match(titulo):
        entry["content"] = _recomendaciones_md(_codigo(titulo))
    elif re.search(r"Concepto de la DATBC", titulo, re.I):
        entry["content"] = _concepto_md(escenario)
    elif _ACTIVIDADES_VERDES.search(titulo):
        entry["content"] = _actividades_md(escenario)
    else:
        entry["content"] = _generico_md(titulo, escenario)
    return entry


def _markdown(steps: list[dict]) -> str:
    partes = []
    for s in steps:
        if s.get("output_mode") == "table":
            cols = s["table"]["columns"]
            filas = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            filas += ["| " + " | ".join(str(r.get(c, "") or "") for c in cols) + " |" for r in s["table"]["rows"]]
            partes.append(f"## {s['title']}\n\n" + "\n".join(filas))
        else:
            partes.append(f"## {s['title']}\n\n{s['content']}")
    return "\n\n".join(partes)


# ---------------------------------------------------------------------------
# Operación, documentos y corrida
# ---------------------------------------------------------------------------

def _documento(owner, project, slug, name, tag):
    doc, _ = Document.objects.get_or_create(
        slug=slug,
        defaults={
            "owner": owner, "name": name, "chunking_status": "done",
            "extracted_text": f"Documento de prueba inventado: {name}.",
        },
    )
    doc.evidence_tags.add(*EvidenceTag.objects.filter(organization=project.organization, slug=tag))
    ProjectDocument.objects.get_or_create(project=project, document=doc, defaults={"added_by": owner})
    return doc


def _corrida(project, skill, owner, stage: str, escenario: dict) -> SkillExecution:
    # Se reescribe cada vez, así sigue a los pasos del workflow si cambiaron. En
    # el lugar y no borrándola: un informe del ejecutivo ya empezado (guiado o
    # editado) apunta a esta corrida y la perdería.
    steps = [_entrada(step, escenario) for step in skill.steps.order_by("position")]
    ahora = timezone.now()
    valores = {
        "owner": owner,
        "status": ExecutionStatus.COMPLETED,
        "output": _markdown(steps),
        "output_structured": {"steps": steps},
        "steps_completed": len(steps),
        "current_step_position": None,
        "error_message": "",
        "started_at": ahora, "finished_at": ahora, "last_progress_at": ahora,
        "metadata": {"seed": SEED_MARK},
    }
    sembradas = SkillExecution.objects.filter(
        project=project, skill=skill, stage=stage, metadata__seed=SEED_MARK,
    ).order_by("id")
    corrida = sembradas.first()
    if corrida is None:
        return SkillExecution.objects.create(skill=skill, project=project, stage=stage, **valores)
    sembradas.exclude(pk=corrida.pk).delete()
    for campo, valor in valores.items():
        setattr(corrida, campo, valor)
    corrida.save()
    return corrida


def sembrar(caf, owner, skills: dict) -> list[Project]:
    """Las operaciones de prueba de IDO y DEC. ``skills``: el informe de cada etapa."""
    proyectos = []
    for stage, escenario in ESCENARIOS.items():
        project, _ = Project.objects.get_or_create(
            slug=escenario["slug"],
            defaults={"owner": owner, "organization": caf, "name": escenario["name"]},
        )
        project.name = escenario["name"]
        project.organization = caf
        project.context_notes = {**escenario["notes"], "estado": stage}

        recorrido = ["pre_originacion", "ido_cci"] + (["dec"] if stage == "dec" else [])
        if "previous_doc" in escenario:
            slug, name, tag = escenario["previous_doc"]
            _documento(owner, project, slug, name, tag)
        principal = _documento(owner, project, escenario["doc_slug"], escenario["doc_name"], escenario["principal_tag"])
        project.blueprint_document = principal
        project.save()

        if not project.stage_transitions.exists():
            anterior = ""
            for etapa in recorrido:
                ProjectStageTransition.objects.create(
                    project=project, from_stage=anterior, to_stage=etapa, created_by=owner, launches=[],
                )
                anterior = etapa

        # En DEC la operación pasó por IDO: tiene también el informe de IDO.
        if stage == "dec":
            _corrida(project, skills["ido_cci"], owner, "ido_cci", escenario)
        _corrida(project, skills[stage], owner, stage, escenario)
        proyectos.append(project)
    return proyectos
