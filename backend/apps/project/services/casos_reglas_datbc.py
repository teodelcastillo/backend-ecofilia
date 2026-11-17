"""
La tabla de casos de las reglas de la DATBC: cada combinación de valores de
los CT de un objetivo, con la determinación que sale y los CT que no aplican.

Las reglas viven dos veces: acá (`caf_criteria`, la referencia) y en el
portal (`ecofiliaV2/lib/caf-alineacion.ts`), que las necesita para recalcular
en vivo mientras el ejecutivo completa el guiado. Para que no diverjan, los
dos lados se prueban contra esta misma tabla, guardada como JSON en cada repo:
`apps/project/tests/fixtures/reglas_datbc.json` y
`ecofiliaV2/tests/fixtures/caf/reglas-datbc.json`.
"""
from __future__ import annotations

import itertools

from apps.project.services import caf_criteria

A1 = [
    "Riesgo bajo", "Riesgo medio", "Riesgo alto", "Indeterminado",
    "Sin análisis de riesgos (AR)", "No aplica análisis de riesgos (AR)", "",
]
A2 = ["Suficientes", "No suficientes", "Información insuficiente", "No aplica", ""]
CONSISTENCIA = ["Consistencia", "Potencialmente inconsistente", "Inconsistente", ""]
M1 = ["Sin actividades no alineadas", "Contiene actividades no alineadas", ""]
M2 = ["Contiene actividades alineadas", "Sin actividades alineadas", ""]
M5 = ["No persisten riesgos de transición", "Persisten riesgos de transición", ""]

DESCRIPCION = (
    "Casos de las reglas de la DATBC (CT -> objetivo y CT que no aplican), generados desde la "
    "referencia del backend (apps/project/services/caf_criteria.py). Los tests del front "
    "(tests/unit/caf-alineacion.test.ts) y del back (apps/project/tests/test_reglas_datbc_casos.py) "
    "leen este mismo archivo: si las reglas cambian, se regenera con "
    "`python manage.py generar_casos_reglas_datbc` y se copia a los dos repos."
)


def casos() -> dict:
    objetivo_1 = []
    for a1, a2, a3 in itertools.product(A1, A2, CONSISTENCIA):
        valor, _ = caf_criteria.determinar_objetivo_1(a1, a2, a3)
        objetivo_1.append({
            "entrada": {"ct_a1": a1, "ct_a2": a2, "ct_a3": a3},
            "valor": valor,
            "no_aplican": ["ct_a2"] if caf_criteria.ct_a2_no_aplica(a1) else [],
        })
    objetivo_2 = []
    for valores in itertools.product(M1, M2, CONSISTENCIA, CONSISTENCIA, M5):
        valor, _, no_aplican = caf_criteria.determinar_objetivo_2(*valores)
        objetivo_2.append({
            "entrada": dict(zip(caf_criteria.CT_OBJETIVO_2, valores)),
            "valor": valor,
            "no_aplican": sorted(no_aplican),
        })
    return {"_descripcion": DESCRIPCION, "objetivo_1": objetivo_1, "objetivo_2": objetivo_2}
