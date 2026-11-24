# Workflow DEC: estructura para el Studio

El informe de Ecofilia de DEC (`dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion`)
tiene su propio análisis: sus propias instrucciones, formato y evidencia. Lo que comparte con
el de IDO es la Sección B (contexto), copiada tal cual, y la letra de cada sección en el título
de los pasos, que es lo que usa la plantilla DEC para ubicarlos. Estructura definitiva
(acordada el 20/11/2026). La copia local (`apps/user/seed_local/workflows/dec-iet-….json`) la
sigue.

## Pasos, en este orden de posición

| Pos. | Paso | Notas |
|---|---|---|
| 1–5 | B.1 … B.5, contexto de clima y biodiversidad | Copia exacta de B.1–B.5 de IDO, **mismos títulos** |
| 6 | A.3. Misión de Evaluación | Etiqueta `mision-evaluacion` |
| 7 | A.4. Antecedentes para el análisis (progreso con respecto al IDO) | Etiquetas `ido` e `iet-datbc-ido`, además de las propias |
| 8 | C.1. Evaluación de los co-beneficios ambientales, climáticos y de biodiversidad | — |
| 9–11 | D.1.1–D.1.3, CT A1–A3 | Con la línea de cierre |
| 12 | D.1.4. Determinación de Alineación con el Objetivo 1 | Historial: posiciones 9–11 |
| 13–17 | D.2.1–D.2.5, CT M1–M5 | Con la línea de cierre |
| 18 | D.2.6. Determinación de Alineación con el Objetivo 2 | Historial: posiciones 13–17 |
| 19 | E.1. Recomendaciones para fortalecer la Alineación… | — |
| 20 | E.2. Concepto de la DATBC | — |

En el informe, el portal agrega al cierre de la Sección E las tablas del ejecutivo y el E.4
(Registro y Trazabilidad, con las opciones de DEC).

### Reglas que el código da por hechas

- **B.1–B.5 van primero (posiciones 1 a 5) y con los mismos títulos que en IDO.** Cuando la
  operación hizo su IDO en Ecofilia, la corrida DEC arranca con esos cinco pasos ya escritos,
  copiados del informe IDO, y no los vuelve a generar. El motor sólo puede reusar pasos del
  principio: si A.3/A.4 van antes, el DEC no hereda nada y corre el contexto completo. En el
  informe igual salen después de la Sección A, porque la plantilla ordena por código.
- **La letra del título decide la sección** (A, B, C, D, E). Las secciones que escribe el
  ejecutivo con el guiado se reconocen por lo que dicen (CT, determinación, co-beneficios,
  recomendaciones, concepto), así que el número exacto dentro de cada sección es libre.
- **El IET IDO emitido se vincula solo** con la etiqueta `iet-datbc-ido` al lanzar el DEC.
- **El IET IDO no es evidencia** (24/11/2026). Al análisis le llega aparte de la base
  documental, como texto rotulado «informe de una etapa anterior · no es evidencia», sin
  citas, y con reglas que pone el sistema: usarlo sólo para confirmar o cambiar lo del IDO
  con los documentos de la etapa, nunca como fuente ni justificación. Vale para el
  vinculado automáticamente y para cualquier documento con la etiqueta `iet-datbc-ido`.
  Aun así conviene pedir `iet-datbc-ido` sólo en A.4: en C.1 y en los CT no hace falta.

### Cierre de cada CT (IDO y DEC)

Al final de las instrucciones de formato de cada CT va:

```
Cerrá la sección con estas dos líneas, exactamente con este formato y sin nada después:
**Conclusión:** una sola de estas opciones, escrita igual: «…» / «…»
**Información faltante:** lo que faltaría para confirmar la conclusión, o «Ninguna».
```

Las opciones de cada CT son exactamente las del portal (`apps/project/services/caf_criteria.py`):

| CT | Opciones |
|---|---|
| A1 | Riesgo bajo · Riesgo medio · Riesgo alto · Indeterminado · No aplica análisis de riesgos (AR) · Sin análisis de riesgos (AR) |
| A2 | Suficientes · No suficientes · Información insuficiente · No aplica |
| A3, M3, M4 | Consistencia · Potencialmente inconsistente · Inconsistente |
| M1 | Contiene actividades no alineadas · Sin actividades no alineadas |
| M2 | Contiene actividades alineadas · Sin actividades alineadas |
| M5 | No persisten riesgos de transición · Persisten riesgos de transición |

Con ese cierre, la conclusión de Ecofilia por criterio se lee del informe visible. Así hay
conclusiones de Ecofilia en DEC, que no tiene S.1, y S.1 deja de hacer falta en IDO.

## Decisiones tomadas (20/11/2026)

- El IAS de CESAS ya no es un paso del DEC (se quitó en producción).
- Las determinaciones quedan dentro de la Sección D (D.1.4 y D.2.6), como en el análisis DEC;
  recomendaciones y concepto son E.1 y E.2.
