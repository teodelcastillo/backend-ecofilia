"""
Alta del workflow que redacta el Informe de Evaluación Técnica (IET) de la DATBC.

Se crea como skill nuevo en vez de reescribir el existente de Sofía
("Evaluación de Alineación - Enfoque de Transacción"), para poder comparar
salidas antes de jubilar el viejo y para no alterar corridas en curso.

De ese skill se hereda la configuración afinada —system prompt, modelo,
temperatura y parámetros de recuperación— y se reemplaza la estructura de
pasos, que ahora sigue el documento "Plantilla_IET_IDO DATBC": las secciones
B (contexto de clima y biodiversidad), D (alineación con el Acuerdo de París)
y E (resumen). Las secciones A y C no se generan acá: A sale de los datos de
la operación y C la completa el ejecutivo.

El contexto de la operación (país, monto, objetivo, componentes y documento
principal) lo inyecta el motor en cada paso — ver
``apps.skill.operation_context`` —, así que las instrucciones de los pasos no
lo repiten.
"""
from django.db import migrations

NEW_SLUG = "caf-iet-datbc-evaluacion-tecnica"
BASE_SLUG_HINTS = ("evaluacion-de-alineacion", "alineacion")

FALLBACK_SYSTEM_PROMPT = (
    "Tu nombre es Ecofilia. Sos un analista técnico de la Dirección de Ambiente, "
    "Cambio Climático y Biodiversidad (DATBC) de CAF, especializado en finanzas "
    "climáticas y alineación con el Acuerdo de París.\n\n"
    "Redactás secciones de un Informe de Evaluación Técnica (IET) que se incorpora "
    "al Informe de Originación de la operación. Escribís en español, en registro "
    "técnico e institucional, sin adjetivación innecesaria.\n\n"
    "Toda afirmación debe apoyarse en los documentos asociados a la operación. "
    "Cuando un dato no esté disponible en ellos, decilo de forma explícita en vez "
    "de completarlo con conocimiento general: un informe que declara un vacío es "
    "útil; uno que lo rellena con supuestos, no."
)

# (título, instrucciones). El orden define `position`.
STEPS: list[tuple[str, str]] = [
    (
        "B.1. Marco de políticas en clima y biodiversidad",
        "Describí el marco de políticas de clima y biodiversidad del país de la "
        "operación: NDC, Plan Nacional de Adaptación, Estrategia de Largo Plazo, "
        "NBSAP y demás instrumentos vigentes que aparezcan en los documentos.\n\n"
        "Para cada instrumento indicá su año y su estado. Citá el documento del que "
        "surge cada dato. Si un instrumento no figura en los documentos, decí que no "
        "se encontró información en lugar de describirlo de memoria.",
    ),
    (
        "B.2. Perfil de vulnerabilidades, riesgos e impactos climáticos",
        "Presentá el perfil de vulnerabilidad y riesgo climático del país o la región "
        "de la operación: amenazas principales, sectores y poblaciones expuestas, e "
        "impactos ya observados o proyectados.\n\n"
        "Priorizá lo que sea relevante para el sector de esta operación. Usá cifras "
        "solo si aparecen en los documentos, citando la fuente.",
    ),
    (
        "B.3. Perfil de emisiones de GEI del país",
        "Describí el perfil de emisiones de gases de efecto invernadero del país: "
        "emisiones totales, principales sectores emisores y tendencia, según los "
        "documentos disponibles.\n\n"
        "Señalá explícitamente el peso del sector en el que se enmarca esta operación. "
        "Indicá el año de referencia de cada cifra.",
    ),
    (
        "B.4. Perfil de conservación de biodiversidad y ecosistemas",
        "Describí el estado de conservación de la biodiversidad y los ecosistemas "
        "relevantes para la operación: ecosistemas estratégicos presentes, áreas "
        "protegidas, especies amenazadas y presiones principales.\n\n"
        "Si la ubicación geográfica de la operación está definida, acotá el análisis a "
        "esa zona. Si no lo está, decilo y mantené el análisis a escala nacional.",
    ),
    (
        "B.5. Elementos clave para el análisis de la operación",
        "Conectá el contexto de las subsecciones anteriores con esta operación en "
        "particular.\n\n"
        "Explicá de qué manera la operación se relaciona con las metas de la NDC, el "
        "NAP, la NBSAP y el PANCD del país: a cuáles contribuye, con cuáles tiene "
        "tensión y cuáles no le aplican. Cerrá señalando los aspectos que la "
        "evaluación técnica deberá mirar con más detalle.",
    ),
    (
        "D.1. Enfoque de evaluación aplicado",
        "Determiná si corresponde el enfoque DE TRANSACCIÓN o DE CLIENTE, según el "
        "instrumento financiero de la operación.\n\n"
        "Indicá el enfoque elegido y justificalo en dos o tres oraciones a partir de la "
        "modalidad de la operación. Si el instrumento no está claro en los documentos, "
        "señalalo y explicá qué información falta para decidirlo.",
    ),
    (
        "D.2. CT A1 — Análisis de riesgos climáticos",
        "Identificá el nivel de riesgo climático preliminar de la operación definido por "
        "CESAS.\n\n"
        "Presentá el resultado como una tabla de dos columnas (Nivel de riesgo | "
        "Fuente) y marcá el nivel que corresponda entre: Bajo, Medio, Alto, "
        "Indeterminado, Sin evaluación de riesgos, No aplica (PBLs, SWAps), o Sin "
        "información de CESAS al momento de la evaluación.\n\n"
        "Especificá la fuente del dato (APRC, IAS, opinión técnica de CESAS u otra). "
        "Si no hay información de CESAS en los documentos, marcá esa última opción: no "
        "infieras el nivel de riesgo por tu cuenta.",
    ),
    (
        "D.2. CT A2 — Análisis de medidas",
        "Identificá las medidas ya incluidas en la operación y las propuestas por CESAS "
        "u otras unidades para gestionar, mitigar o reducir sus riesgos climáticos.\n\n"
        "Listá las medidas encontradas y analizá si permiten reducir de manera "
        "aceptable los riesgos climáticos y alcanzar el Objetivo 1 (desarrollo "
        "resiliente al clima). Concluí con una de estas dos opciones, textual: 'Las "
        "medidas permiten reducir de manera aceptable los riesgos climáticos' o 'Las "
        "medidas NO permiten reducir de manera aceptable los riesgos climáticos'.\n\n"
        "Si es factible, sugerí medidas adicionales como recomendación.",
    ),
    (
        "D.2. CT A3 — Consistencia con los instrumentos nacionales de adaptación",
        "Analizá si la operación es consistente —o al menos no inconsistente— con los "
        "instrumentos nacionales de adaptación aplicables: componente de adaptación de "
        "la NDC, Plan Nacional de Adaptación, Comunicación de Adaptación y componente "
        "de adaptación de la Estrategia de Largo Plazo.\n\n"
        "Analizá cada instrumento por separado, citando el documento. Cerrá eligiendo "
        "una de estas cuatro determinaciones, textual: 'Consistencia o al menos no "
        "inconsistencia con los instrumentos nacionales de adaptación'; 'Consistencia o "
        "al menos no inconsistencia con los instrumentos nacionales de adaptación, con "
        "brechas'; 'Inconsistencia explícita con los instrumentos nacionales de "
        "adaptación'; 'Potencialmente inconsistente con los instrumentos nacionales de "
        "adaptación'.",
    ),
    (
        "D.2. Determinación y recomendaciones — Objetivo 1: Desarrollo resiliente al clima",
        "Integrá los criterios técnicos A1, A2 y A3 en la determinación del Objetivo 1.\n\n"
        "Indicá la determinación —Alineada, Con Brechas para su Alineación, o No "
        "Alineada— y justificala apoyándote en los tres criterios anteriores, sin "
        "repetirlos por extenso.\n\n"
        "Agregá después las recomendaciones preliminares para fortalecer el "
        "alineamiento, como lista. Cada recomendación debe ser accionable e indicar a "
        "quién está dirigida (CESAS, el área de negocio o el cliente).",
    ),
    (
        "D.3. CT M1 — Actividades consideradas no alineadas",
        "Determiná si la operación incluye alguna de estas actividades: minería de "
        "carbón, generación de electricidad a base de carbón, extracción de turba, o "
        "generación de electricidad a base de turba.\n\n"
        "Revisá los componentes de la operación uno por uno. Si encontrás alguna de esas "
        "actividades, nombrá el componente que la contiene y concluí que la operación "
        "se encuentra contenida en la lista de actividades no alineadas. Si no, "
        "concluí que NO se encuentra contenida y que corresponde continuar al CT M2.",
    ),
    (
        "D.3. CT M2 — Actividades alineadas de manera directa",
        "Determiná si la operación concuerda con alguna categoría de la lista de "
        "actividades consideradas alineadas de manera directa con el objetivo de "
        "desarrollo bajo en emisiones de GEI (energía, manufactura, agricultura y usos "
        "de la tierra, gestión de residuos, agua y saneamiento, transporte, edificios e "
        "instalaciones públicas, TIC, investigación y desarrollo, servicios, y "
        "actividades multisectoriales).\n\n"
        "Si concuerda, indicá la categoría y la tipología específica, y verificá que se "
        "cumplan las condiciones y lineamientos asociados. Si no concuerda con ninguna, "
        "decilo explícitamente y señalá que corresponde continuar al CT M3.",
    ),
    (
        "D.3. CT M3 — Consistencia con la NDC",
        "Analizá la consistencia de la operación con la NDC del país aplicable al "
        "período de implementación del proyecto.\n\n"
        "Revisá los componentes sectoriales de la NDC, sus metas de reducción de "
        "emisiones y las actividades priorizadas, y contrastalos con los componentes de "
        "la operación. Cerrá con una de estas tres determinaciones, textual: "
        "'Consistencia o al menos no inconsistencia con la NDC'; 'Inconsistencia con la "
        "NDC'; 'Potencialmente inconsistente con la NDC'.\n\n"
        "Si hay inconsistencia o potencial inconsistencia, justificala en detalle.",
    ),
    (
        "D.3. CT M4 — Consistencia con trayectorias de desarrollo bajo en emisiones",
        "Analizá la consistencia de la operación con las trayectorias de desarrollo bajo "
        "en emisiones de GEI.\n\n"
        "Empezá indicando si el país cuenta con Estrategia de Largo Plazo (ELP) según "
        "los documentos. Si la tiene, analizá la consistencia contra ella. Si no la "
        "tiene, o si el resultado es 'potencialmente inconsistente', analizá contra las "
        "guías sectoriales de evaluación de alineación de CAF que correspondan al sector "
        "de la operación.\n\n"
        "Dejá explícito cuál de los dos caminos aplicaste y por qué, y cerrá con la "
        "determinación resultante. Justificá en detalle toda inconsistencia o potencial "
        "inconsistencia.",
    ),
    (
        "D.3. CT M5 — Análisis de riesgos de transición",
        "Determiná si la operación incluye actividades o sectores identificados con "
        "riesgo de transición y, en ese caso, analizá esos riesgos según las guías "
        "técnicas sectoriales adoptadas por la GACBP.\n\n"
        "Concluí con una de estas dos opciones, textual: 'No persisten riesgos de "
        "transición' o 'Persisten riesgos de transición'. Si persisten, explicá cuáles "
        "y en qué componentes.",
    ),
    (
        "D.3. Determinación y recomendaciones — Objetivo 2: Desarrollo bajo en emisiones de GEI",
        "Integrá los criterios técnicos M1 a M5 en la determinación del Objetivo 2.\n\n"
        "Indicá la determinación —Alineada, Con Brechas para su Alineación, o No "
        "Alineada— y justificala señalando qué criterio resultó determinante.\n\n"
        "Agregá después las recomendaciones preliminares para fortalecer el "
        "alineamiento, como lista, cada una accionable y con su destinatario.",
    ),
    (
        "E.1. Análisis preliminar de alineación con el Acuerdo de París",
        "Resumí las dos determinaciones en una tabla de tres columnas: Objetivo | "
        "Determinación | Justificación breve.\n\n"
        "La primera fila es el objetivo de desarrollo resiliente al clima y la segunda "
        "el de desarrollo con bajas emisiones de GEI. En Determinación usá exactamente "
        "'Alineada', 'Con brechas para su alineación' o 'No Alineada', tomándolas de los "
        "pasos anteriores sin volver a analizarlas.\n\n"
        "Debajo de la tabla, agregá las recomendaciones para fortalecer el alineamiento, "
        "consolidando las de ambos objetivos sin repetirlas.",
    ),
    (
        "E.3. Concepto de la DATBC",
        "Redactá el concepto de cierre de la DATBC sobre la operación, integrando todo "
        "lo analizado.\n\n"
        "Cubrí, en este orden: si la operación incluye componentes con potencial de "
        "contribuir al financiamiento verde y en cuáles de los tres objetivos "
        "(adaptación, mitigación, biodiversidad y otros objetivos ambientales), con su "
        "argumentación; las recomendaciones para aumentar los cobeneficios; las dos "
        "determinaciones de alineación con su fundamento; y los aspectos que será "
        "necesario indagar durante la misión de evaluación.\n\n"
        "Es el texto que leen quienes no van a leer el resto del informe: prosa continua, "
        "sin listas ni encabezados, autocontenido.",
    ),
]


def _base_skill(Skill):
    """Skill de referencia del que heredar la configuración afinada."""
    for hint in BASE_SLUG_HINTS:
        base = (
            Skill.objects.filter(slug__icontains=hint, skill_type="copilot")
            .order_by("id")
            .first()
        )
        if base:
            return base
    return None


def create_iet_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    if Skill.objects.filter(slug=NEW_SLUG).exists():
        return

    base = _base_skill(Skill)

    skill = Skill.objects.create(
        owner=None,
        skill_type="copilot",
        name="IET DATBC — Evaluación Técnica de la Operación",
        slug=NEW_SLUG,
        description=(
            "Redacta las secciones del Informe de Evaluación Técnica de la DATBC que "
            "se generan por análisis: contexto de clima y biodiversidad (B), "
            "alineación con el Acuerdo de París (D) y resumen (E)."
        ),
        allowed_contexts=["project"],
        system_prompt=(base.system_prompt if base else FALLBACK_SYSTEM_PROMPT),
        prompt_template="",
        # El modelo efectivo lo resuelve `effective_chat_model` en tiempo de
        # request según LLM_PROVIDER, así que este valor es solo el de partida.
        model=(base.model if base else "gpt-4o-mini"),
        temperature=(base.temperature if base else 0.3),
        retrieval_strategy=(base.retrieval_strategy if base else "global"),
        k_per_doc=(base.k_per_doc if base else 2),
        total_limit=(base.total_limit if base else 12),
    )

    SkillStep.objects.bulk_create(
        [
            SkillStep(
                skill=skill,
                title=title,
                instructions=instructions,
                position=position,
                step_type="instruction",
                output_mode="text",
                approval_required=False,
            )
            for position, (title, instructions) in enumerate(STEPS, start=1)
        ]
    )


def remove_iet_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Skill.objects.filter(slug=NEW_SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0017_workflow_step_skill_ref_and_docs"),
    ]

    operations = [
        migrations.RunPython(create_iet_workflow, remove_iet_workflow),
    ]
