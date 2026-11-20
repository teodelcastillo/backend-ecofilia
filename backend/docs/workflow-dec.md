# Workflow DEC: estructura para el Studio

El informe de Ecofilia de DEC (`dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion`)
tiene que tener **la misma estructura y los mismos códigos que el de IDO**
(`caf-iet-datbc-evaluacion-tecnica`). Con los mismos códigos, la ficha, el guiado, la plantilla,
el tablero y la comparación IA/ejecutivo leen las dos etapas igual, sin casos especiales.

Los prompts se cambian a mano en el Studio. La copia local
(`apps/user/seed_local/workflows/dec-iet-….json`) ya tiene esta estructura, armada a partir de
la de producción del 2026-11-20, para probar en local antes de tocar producción.

## Pasos, en este orden de posición

| Pos. | Paso | De dónde sale | Etiquetas |
|---|---|---|---|
| 1–5 | B.1 … B.5, contexto de clima y biodiversidad | Copia exacta de B.1–B.5 de IDO. Donde IDO pide `ido`, se suma `dec` | Las de IDO (+ `dec`) |
| 6 | A.3. Misión de Evaluación | A.3 actual | `mision-evaluacion` |
| 7 | A.4. Antecedentes para el análisis (progreso con respecto al IDO) | A.5 actual, más un párrafo que nombra el documento IDO y el IET IDO emitido | `documento-operacion`, `iet-datbc-ido`, `ido`, `dec`, `ias`, `mision-evaluacion` |
| 8 | A.5. Resumen del Informe Ambiental y Social de CESAS | A.4 actual (**decisión pendiente**) | `ias` |
| 9 | C.1. Evaluación de los co-beneficios ambientales, climáticos y de biodiversidad | B.1 actual | Las actuales |
| 10–12 | D.1.1–D.1.3, CT A1–A3 | C.1.1–C.1.3 actuales, con el cierre de abajo | Las actuales |
| 13–17 | D.2.1–D.2.5, CT M1–M5 | C.2.1–C.2.5 actuales, con el cierre de abajo | Las actuales |
| 18 | E.1. Determinación de Alineación con el Objetivo 1 | C.1.4 actual; historial: posiciones 10–12 | — |
| 19 | E.2. Determinación de Alineación con el Objetivo 2 | C.2.6 actual; historial: posiciones 13–17 | — |
| 20 | E.3. Recomendaciones para fortalecer la Alineación… | D.1 actual | — |
| 21 | E.5. Concepto de la DATBC | D.2 actual (**decisión pendiente**: dónde va en la plantilla) | — |

### Reglas que el código da por hechas

- **B.1–B.5 van primero (posiciones 1 a 5) y con los mismos títulos que en IDO.** Cuando la
  operación pasó por IDO en Ecofilia, la corrida DEC arranca con esos cinco pasos ya escritos,
  copiados del IDO, y no los vuelve a generar. Si no son las primeras posiciones o los títulos
  no coinciden, el DEC corre todo y queda en el log. En el informe salen después de la Sección A,
  porque la plantilla ordena por código, no por posición.
- **Los títulos de las determinaciones no dicen «preliminar»** en DEC.
- **La etiqueta de la Misión es `mision-evaluacion`.** `mision-de-evaluacion` no existe, así que
  el paso corría sin documentos.
- **`iet-datbc-ido` es el IET IDO emitido.** Cuando la operación pasó por IDO en Ecofilia, el
  informe emitido se vincula solo a la operación con esa etiqueta. Si el IDO se hizo por fuera,
  se sube en el alta DEC con esa etiqueta.

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

## Decisiones pendientes

1. **«Resumen del Informe Ambiental y Social de CESAS»**: no está en la lista de la plantilla DEC.
   Hay que decidir si se elimina, si se integra en otro paso (por ejemplo en A.4) o si queda como A.5.
2. **«Concepto de la DATBC»**: en qué lugar de la plantilla DEC va. En la copia local quedó como E.5.
