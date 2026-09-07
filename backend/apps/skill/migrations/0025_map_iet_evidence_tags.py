"""
Declara, paso por paso, qué evidencia pide el IET DATBC.

Hasta ahora sus dieciocho pasos leían el expediente entero: no había forma de
decir otra cosa que no fuera enumerar documentos concretos, y los slugs de las
NDC cambian con cada operación. El resultado es que el criterio de consistencia
con la NDC leía, además de la NDC, el préstamo, las salvaguardas y todo lo
demás — y que el ejecutivo tenía que tildar documentos a mano en cada corrida
para evitarlo.

Al hacer el mapeo aparece algo que conviene dejar anotado: **la mitad de los
pasos no necesita ningún documento de biblioteca**. Los que integran criterios
previos y los que revisan los componentes de la operación trabajan sobre el
documento principal y sobre lo ya redactado. Que hoy reciban el expediente
completo no los ayuda: les mete material que no van a usar y del que igual
podrían terminar citando.

Los pasos se ubican por el prefijo de su título ("B.1.", "D.2. CT A1"), que es
la misma llave con la que el visor arma el informe. Un paso renombrado no se
toca: es preferible dejarlo leyendo todo —su comportamiento de siempre— que
asignarle por posición una evidencia que quizá no le corresponda.
"""
from django.db import migrations

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"

# (prefijo del título, selección, etiquetas)
MAPPING: list[tuple[str, str, list[str]]] = [
    # ── Sección B: contexto de clima y biodiversidad del país ──
    ("B.1.", "tagged", ["ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap"]),
    ("B.2.", "tagged", ["nap", "comunicacion-adaptacion", "ndc"]),
    ("B.3.", "tagged", ["ndc", "lts", "inventario-gei"]),
    ("B.4.", "tagged", ["nbsap", "pancd"]),
    ("B.5.", "tagged", ["ndc", "nap", "nbsap", "pancd"]),
    # ── Sección D.2: objetivo de desarrollo resiliente al clima ──
    # El enfoque depende del instrumento financiero: está en el documento de
    # la operación y en ningún otro lado.
    ("D.1.", "blueprint_only", []),
    ("D.2. CT A1", "tagged", ["salvaguardas-cesas", "documento-operacion"]),
    ("D.2. CT A2", "tagged", ["salvaguardas-cesas", "documento-operacion"]),
    ("D.2. CT A3", "tagged", ["ndc", "nap", "comunicacion-adaptacion", "lts"]),
    # Integra A1, A2 y A3: su material son los pasos previos.
    ("D.2. Determinación", "blueprint_only", []),
    # ── Sección D.3: objetivo de desarrollo bajo en emisiones ──
    # La lista de actividades no alineadas está en la propia instrucción; lo
    # que hay que revisar son los componentes de la operación.
    ("D.3. CT M1", "blueprint_only", []),
    ("D.3. CT M2", "tagged", ["metodologia-caf"]),
    ("D.3. CT M3", "tagged", ["ndc"]),
    ("D.3. CT M4", "tagged", ["lts", "metodologia-caf"]),
    ("D.3. CT M5", "tagged", ["metodologia-caf"]),
    ("D.3. Determinación", "blueprint_only", []),
    # ── Sección E: resumen y concepto ──
    ("E.1.", "blueprint_only", []),
    ("E.3.", "blueprint_only", []),
]


def map_evidence(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return

    steps = list(SkillStep.objects.filter(skill=skill))
    for prefix, selection, tags in MAPPING:
        matches = [s for s in steps if (s.title or "").strip().startswith(prefix)]
        if len(matches) != 1:
            # Cero: el paso se renombró. Más de uno: el prefijo dejó de ser
            # unívoco. En ambos casos no hay una asignación evidente y el paso
            # se queda como estaba.
            continue
        step = matches[0]
        step.evidence_selection = selection
        step.evidence_tags = tags
        step.save(update_fields=["evidence_selection", "evidence_tags"])


def unmap_evidence(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return
    SkillStep.objects.filter(skill=skill).update(
        evidence_selection="all", evidence_tags=[]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0024_step_evidence_selection"),
    ]

    operations = [
        migrations.RunPython(map_evidence, unmap_evidence),
    ]
