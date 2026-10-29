"""
El catálogo de Criterios y Actividades Elegibles de Financiamiento Verde de CAF.

Cuatro niveles: objetivo (A Adaptación, B Medio ambiente y biodiversidad,
M Mitigación), categoría (B.1), subcategoría (B.1.1) y actividad (B.1.1.1). El
código que se informa por componente es siempre el de la actividad.

Vive en ``data/caf_actividades_elegibles.json``, tal como lo pasó CAF (sin las
marcas de nota al pie). El catálogo trae dos actividades distintas con el código
``M.8.1.2``: se conservan las dos, con el mismo código, hasta que CAF lo aclare.

Los pasos de la IET que lo usan (C.1 y S.2, y el C.1 del guiado) lo reciben
como texto de referencia en sus instrucciones (``skill/0047``).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "data" / "caf_actividades_elegibles.json"


@lru_cache(maxsize=1)
def catalogo() -> list[dict]:
    with _PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def codigos_de_actividad() -> list[str]:
    """Los códigos de cuarto nivel, sin repetir, en el orden del catálogo."""
    out: list[str] = []
    for row in catalogo():
        if row["nivel"] == 4 and row["codigo"] not in out:
            out.append(row["codigo"])
    return out


def como_referencia() -> str:
    """El catálogo como texto para un prompt: los niveles 1 a 3 como títulos y
    cada actividad con su código."""
    lineas = []
    for row in catalogo():
        if row["nivel"] == 1:
            lineas.append(f"\n{row['codigo']}. {row['nombre']}")
        elif row["nivel"] < 4:
            lineas.append(f"{row['codigo']} {row['nombre']}")
        else:
            lineas.append(f"- {row['codigo']} {row['nombre']}")
    return "\n".join(lineas).strip()
