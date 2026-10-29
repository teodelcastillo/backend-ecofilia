# Contrato de API — plan de instituciones (fases 0 a 5)

Este archivo es **la única fuente** del contrato entre el backend y el frontend
para el plan de instituciones. La sesión del back lo escribe **antes** de
implementar cada fase; la del front programa contra lo que dice acá. Si algo
cambia, se cambia en este archivo, en el mismo PR que lo implementa.

| Fase | Qué | Estado del contrato |
|---|---|---|
| 0 | Aislamiento entre instituciones | **Implementado** (PR #7) |
| 1 | Configuración de cada institución en datos | **Implementado** (PR de `fase-1-backend`) |
| 2 | Consola del superadmin | Pendiente |
| 3 | Portal genérico `/{institución}` | Pendiente |
| 4 | Plantilla pública `/app` y portal base sólo superadmin | Pendiente |
| 5 | Alta de instituciones | Pendiente |

---

## Cómo trabajamos sin tocar producción

- `main` es producción en los dos repos. En el back, cada push a `main` construye
  la imagen, **corre las migraciones** y despliega (`.github/workflows/deploy.yml`).
  No se mergea nada a `main` hasta la salida.
- Rama de integración: **`instituciones`**, creada desde `main` en los dos repos.
  Cada fase entra por PR a `instituciones`. Los tests del back corren en cada PR
  (`.github/workflows/tests.yml`); nada se despliega.
- Cada tanto se mergea `main` en `instituciones`, para no dejar afuera los
  arreglos de producción.
- Las previews del front apuntan por defecto a la API de producción: **no se
  usan para probar** este plan.

---

## Entorno local

### Backend

Desde la rama `instituciones` (o la de la fase en curso):

```bash
cd backend
cp docker/template.env docker/.env      # DEBUG=True, POSTGRES_HOST=db, CORS para localhost:3000
docker compose -f docker-compose.dev.yml up --build
#   → API en http://localhost:8000 (http://localhost:8000/health/ responde 200)
#   → las migraciones corren solas al arrancar; la base vive en el volumen pg_dev_data
docker exec -it ecofilia-backend-dev ./manage.py seed_local_instituciones
```

- Nunca con credenciales ni `.env` de producción. `docker/.env` está en `.gitignore`.
- El contrato se prueba sin claves de LLM. Correr un asistente de verdad sí las
  necesita (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` en `docker/.env`), y los
  workflows además necesitan una cola: sin ella el encolado falla y la corrida
  responde 503.
- Para empezar de cero: `docker compose -f docker-compose.dev.yml down -v`.

Verificación rápida por HTTP, sin frontend:

```bash
python scripts/verificar_contrato_local.py http://localhost:8000/api
```

### Frontend

`.env.local` (ya ignorado por `.gitignore`):

```
NEXT_PUBLIC_API_URL=http://localhost:8000/api
```

Sin esa variable el front cae a `https://api.ecofilia.site/api`, **producción**.

### Datos de prueba: `seed_local_instituciones`

Idempotente: se puede correr las veces que haga falta. Se niega a correr con
`DEBUG` falso, con settings de prod o contra una base en RDS.

Deja la base como quedaría producción después de las migraciones de la fase 0
(crea CAF y vuelve a aplicar las migraciones de datos), más una segunda
institución, un usuario público y un superadmin.

> Volver a correrlo **reaplica** la migración de datos de skills: lo que se haya
> hecho público a mano vuelve a su alcance original (las `local-*` se
> restauran tal como las crea el comando).

**Usuarios** — todos con la clave **`ecofilia-local`** (`--password` la cambia):

| Email | Nivel | Institución |
|---|---|---|
| `superadmin@local.test` | superadmin de Ecofilia | — |
| `caf-admin@local.test` | administrador de institución (`role=admin`) | `caf` (restringida) |
| `caf-miembro@local.test` | miembro | `caf` (restringida) |
| `otra-miembro@local.test` | miembro | `otra` (sin restricciones) |
| `publico@local.test` | público | — |

**Datos**

| Qué | Slug | Alcance / institución |
|---|---|---|
| Institución | `caf` | restringida |
| Institución | `otra` | sin restricciones |
| Operación | `local-doc-caf` | de `caf-miembro`, institución `caf` |
| Operación | `local-doc-otra` | de `otra-miembro`, institución `otra` |
| Operación | `local-doc-publico` | de `publico`, sin institución |
| Documento | `doc-caf`, `doc-otra`, `doc-publico` | uno por operación |
| Informe emitido | uno por operación | etapa `ido_cci` |
| Corrida | una en `local-doc-otra` | skill `local-de-otra` |
| Skill | `local-plataforma` | `platform` |
| Skill | `local-publica` | `public` |
| Skill | `local-de-otra` | `organization` / `otra` |
| Skill | `caf-*` (de las migraciones) | `organization` / `caf`; habilitadas para CAF salvo `caf-insumo-ejecutivo` |
| Skill | `local-propia-del-publico` | `user` / dueño `publico` |
| Etiqueta | `propia-de-otra` | institución `otra` |
| Etiqueta | `ido`, `dec`, `ias`, `ibat`, …, `metodologia-caf`, `salvaguardas-cesas` | institución `caf` (11) |
| Etiqueta | `ndc`, `nap`, `lts`, … | globales (las 9 de país) |

**Lo que tiene que ver cada uno** (verificado con `verificar_contrato_local.py`):

| Usuario | `GET /api/skills/` | Operaciones y emitidos | Etiquetas con institución | `evaluation-records` sin org / `caf` / `otra` | `GET /api/skills/local-plataforma/` |
|---|---|---|---|---|---|
| superadmin | todas | las 3 | las de `caf` y `otra` | 400 / 200 / 200 | 200 |
| caf-admin, caf-miembro | 4 `caf-*` + `local-publica` | la de CAF | las 11 de `caf` | 400 / 200 / 403 | 404 |
| otra-miembro | `local-de-otra` + `local-publica` | la suya | `propia-de-otra` | 400 / 403 / 200 | 404 |
| publico | `local-publica` + `local-propia-del-publico` | la suya | ninguna | 400 / 403 / 403 | 404 |

Todos ven las 9 etiquetas globales (las de país). Ninguna etiqueta de CAF le
llega a `otra` ni al público.

---

## Fase 0 — Aislamiento entre instituciones

**Regla:** ninguna información de una institución la ve otra. Cada institución
mejora sus asistentes sólo con sus propios datos.

### Niveles de acceso

| Nivel | Cómo se reconoce |
|---|---|
| Superadmin de Ecofilia | `is_superuser` o `role=admin`, **sin** institución (`apps.user.access.is_ecofilia_admin`) |
| Miembro de institución | `organization` no nulo. Si además `role=admin`, administra su institución |
| Público | sin institución y no superadmin |

Todos los endpoints piden JWT (`Authorization: Bearer <access>`, login en
`POST /api/auth/login/` con `{"email", "password"}`).

### Skills

**Campos nuevos** en la lectura (`GET /api/skills/`, `GET /api/skills/{slug}/`):

| Campo | Tipo | Valores |
|---|---|---|
| `scope` | string | `"platform"` · `"public"` · `"organization"` · `"user"` |
| `organization_slug` | string \| null | slug de la institución cuando `scope="organization"`; si no, `null` |

**Visibilidad** — vale para el listado, el detalle y `run`:

| Quién | Ve |
|---|---|
| superadmin | todas |
| miembro de institución | `public` + `organization` de su institución + sus `user`. Si la institución es `restricted`, de las de su institución sólo las que tiene en `enabled_skills` |
| público | `public` + sus `user` |

`platform` no la ve nadie más que el superadmin. Una skill que no se ve
responde **404** en `GET /api/skills/{slug}/` y en `POST /api/skills/{slug}/run/`.

**Corridas sobre una skill que no se ve.** Si la corrida es visible (por
ejemplo, en una operación compartida) pero la skill no,
`POST /api/skills/executions/{id}/{acción}/` responde **403**. Las acciones son
`resume`, `rerun`, `rerun-from-step`, `approve` y `regenerate-step`.

**Cambiar el alcance** — sólo superadmin:

```
PATCH /api/skills/{slug}/
{"scope": "organization", "organization_slug": "caf"}
```

| Caso | Respuesta |
|---|---|
| superadmin, válido | **200** con la skill completa (misma forma que el `GET`) |
| `scope="organization"` sin `organization_slug`, o con un slug que no existe | **400** `{"organization_slug": [...]}` |
| otro alcance con `organization_slug` | 200; la institución queda en `null` (se ignora el slug) |
| no superadmin | **403** |
| skill que no ve | **404** |

Un PATCH que sólo trae `scope` / `organization_slug` no toca la definición de la
skill.

**Crear** (`POST /api/skills/`): lo que crea un no superadmin nace con
`scope="user"` y `owner` = quien la crea; si manda otro `scope`, **403**. El
superadmin puede mandar `scope` / `organization_slug` con las mismas reglas que
el PATCH; si no los manda, nace `user`.

### Versiones de definición

`GET /api/skills/{slug}/definition-versions/` → cada versión suma
`informed_by_organization_slug` (string \| null).

```
PATCH /api/skills/{slug}/definition-versions/{version_number}/
{"informed_by_organization_slug": "caf"}        // o null
```

| Caso | Respuesta |
|---|---|
| superadmin, válido | **200** con la versión |
| skill `scope="organization"` y el slug no es su institución (incluye `null`) | **400** `{"informed_by_organization_slug": [...]}` |
| falta el campo, o la institución no existe | **400** |
| versión inexistente | **404** |
| no superadmin | **403** |

Las versiones que se crean solas al correr una skill de institución nacen
informadas por esa institución.

### Etiquetas de evidencia

**Campo nuevo** en cada etiqueta: `organization_slug` (string \| null). Con
`null` la etiqueta es global (instrumentos de país); con slug, es de esa
institución. `group` no cambia.

```
GET /api/document/evidence-tags/
GET /api/document/evidence-tags/?organization=caf      // sólo superadmin
```

| Quién | Recibe |
|---|---|
| miembro de institución | globales + las de su institución |
| público | sólo globales |
| superadmin | todas. Con `?organization=<slug>`, las que ve esa institución: globales + las suyas. Un slug inexistente da 404 |

El slug es único **por institución**. Dentro de lo que ve una institución nunca
se repite: una etiqueta de institución no puede llevar el slug de una global.

`GET /api/document/evidence-tags/{slug}/` resuelve dentro de lo que ve el
usuario; si no la ve, **404**.

`document_count` cuenta sólo los documentos que ve quien pregunta (el
superadmin, todos).

**Crear** (`POST /api/document/evidence-tags/` con `{"name", ...}`):

| Quién | Resultado |
|---|---|
| miembro de institución | **201**; la etiqueta es de su institución. Si manda `organization_slug` de otra, **403** |
| superadmin | **201**; global, o de la institución que diga `organization_slug` |
| público | **403** |
| slug repetido dentro de lo que ve la institución de destino | **400** |

Editar y borrar sigue siendo de staff, como antes.

**Resolución de slugs.** Todo slug de etiqueta se resuelve dentro de las
globales más las de la institución de la operación. Aplica a:

- los pasos de skills (`evidence_tags`);
- `PUT /api/projects/{slug}/documents/{doc}/tags/`;
- la etiqueta de etapa del documento principal;
- los instrumentos de país.

En subidas y etiquetado de documentos se resuelve dentro de lo que ve quien
etiqueta. Un slug que no existe en ese conjunto da 400 «Etiquetas
inexistentes».

### Registro de evaluación

```
GET /api/projects/evaluation-records/?organization=<slug>
```

| Caso | Respuesta |
|---|---|
| sin `organization` | **400** |
| no superadmin pidiendo otra institución (o público pidiendo cualquiera) | **403** |
| superadmin con un slug que no existe | **404** |
| válido | **200**, lista de registros de las operaciones visibles de esa institución |

### Otros listados (sin cambios de forma, ya aislados)

| Endpoint | Regla |
|---|---|
| `GET /api/projects/` y detalle | propias, compartidas, y todas las de su institución si es admin de ella. Superadmin: todas |
| `GET /api/skills/executions/` y detalle | propias o de operaciones/repositorios visibles. Detalle ajeno: 404 |
| `GET /api/document/list/` y `GET /api/document/{slug}/` | propios, públicos, compartidos y de operaciones visibles. Detalle ajeno: 404 |
| `GET /api/projects/stage-reports/emitidos/` | los de operaciones visibles y los de su institución. `?organization=` de otra devuelve vacío |

### Migración de datos al desplegar

- Skills `caf-*` → `organization` / CAF. Las demás con dueño → `user`. **Todas
  las demás sin dueño → `platform`.**
- Etiquetas de operación de CAF → CAF (`document/0024`), y también
  `metodologia-caf` y `salvaguardas-cesas` (`document/0025`). Las de país
  quedan globales: son las únicas que ve una institución que no es CAF.

> **Para la salida:** las skills que hoy ven todos (en una base nueva, las 11
> plantillas `ecofilia-*`) quedan en `platform` y dejan de verse. La lista de
> las que se abren al público tiene que estar decidida antes del deploy, para
> abrirlas en el mismo deploy (migración de datos) o en la misma ventana
> (`./manage.py listar_skills_alcance --hacer-publicas ...`).

---

## Fase 1 — Configuración de cada institución en datos

**Objetivo:** que lo que hoy está escrito para CAF en el código (en el back y en
el front) salga de la configuración de cada institución. Para CAF el
comportamiento queda idéntico: una migración le siembra su configuración con
los valores que hoy están en el código.

### Qué se configura

1. **Marca:** nombre visible y logo.
2. **Etapas** de una operación, en orden, con su etiqueta.
3. **Etiqueta del documento principal** por etapa (hoy IDO → `ido`, DEC → `dec`).
4. **Qué asistentes lanza** la operación al entrar a cada etapa (hoy, sólo IDO lanza el IET).
5. **Roles de los workflows del informe:** cuál es el informe de Ecofilia, cuál el
   guiado y cuál la lectura de un informe subido.
6. **Grupos de etiquetas** (hoy `apps/document/evidence_tag_groups.py`).
7. **Criterios de evaluación**, con sus opciones y tonos (hoy duplicados en
   `caf_criteria.py` y `ecofiliaV2/lib/caf-alineacion.ts`).

### Forma de la configuración (versión 1)

```json
{
  "version": 1,
  "branding": {
    "display_name": "CAF",
    "logo_url": "/images/caf-logo.png"
  },
  "stages": [
    {"slug": "pre_originacion", "label": "Pre-originación", "principal_tag": null, "launches": []},
    {"slug": "ido_cci", "label": "IDO-CCI", "principal_tag": "ido",
     "launches": ["caf-iet-datbc-evaluacion-tecnica"]},
    {"slug": "dec", "label": "DEC", "principal_tag": "dec", "launches": []},
    {"slug": "aprobada", "label": "Aprobada", "principal_tag": null, "launches": []}
  ],
  "workflows": {
    "report": "caf-iet-datbc-evaluacion-tecnica",
    "guided": "caf-iet-guiado-ejecutivo",
    "reading": "caf-lectura-informe-ejecutivo"
  },
  "evidence_tag_groups": [
    {"slug": "principal", "label": "Documento principal", "tags": ["ido", "dec"]},
    {"slug": "operacion", "label": "Documentos de la operación",
     "tags": ["ias", "analisis-riesgos", "matriz-resultados", "disenos-proyecto", "ibat", "otros", "documento-operacion"]},
    {"slug": "contexto", "label": "Contexto país",
     "tags": ["ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap", "pancd", "inventario-gei", "ley-cambio-climatico", "reporte-nacional"]}
  ],
  "evaluation": {
    "framework": "caf-datbc",
    "criteria": [
      {"key": "ct_a1", "code": "CT A1", "name": "Análisis de riesgos climáticos", "group": "objetivo_1",
       "is_determination": false,
       "options": [{"value": "Riesgo bajo", "short": "Riesgo bajo", "tone": "ok"}, "…"]},
      "… los 10 criterios de CAF: ct_a1, ct_a2, ct_a3, objetivo_1, ct_m1 … ct_m5, objetivo_2"
    ]
  }
}
```

| Campo | Tipo | Reglas |
|---|---|---|
| `version` | int | `1` |
| `branding.display_name` | string | no vacío; por defecto, el `name` de la institución |
| `branding.logo_url` | string | URL absoluta o ruta del front (`/images/...`); puede ser `""` |
| `stages[]` | lista | orden = orden del ciclo; puede ser vacía |
| `stages[].slug` | slug | único; es el valor de `context_notes.estado` |
| `stages[].label` | string | no vacío |
| `stages[].principal_tag` | slug \| null | etiqueta que existe para la institución (globales + las suyas) |
| `stages[].launches` | lista de slugs de skill | cada una `public` o de esta institución |
| `workflows.report` / `.guided` / `.reading` | slug de skill \| null | ídem; `null` = la institución no tiene ese rol |
| `evidence_tag_groups[]` | lista | `slug` único; una etiqueta en a lo sumo un grupo; cada etiqueta existe para la institución. Una etiqueta sin grupo cae en `"otro"` |
| `evaluation.framework` | string | `"caf-datbc"` o `""`. Con `caf-datbc`, los criterios tienen que ser exactamente los 10 de CAF: las reglas de determinación están en código y dependen de esas claves |
| `evaluation.criteria[]` | lista | `key` única; `group` es la clave del criterio-determinación al que pertenece (o `null`); `options[].tone` ∈ `ok`, `atencion`, `brecha`, `critico`, `pendiente`, `neutro` |

Los roles de la etapa se derivan, no se configuran aparte. Una etapa tiene
**informe de Ecofilia** si sus `launches` incluyen `workflows.report`; hoy, sólo
`ido_cci`. Ahí se ofrecen las vías editado y guiado.

### Configuración base

La usan las operaciones **sin institución** y las instituciones que todavía no
tienen configuración propia:

```json
{
  "version": 1,
  "branding": {"display_name": "Ecofilia", "logo_url": ""},
  "stages": [],
  "workflows": {"report": null, "guided": null, "reading": null},
  "evidence_tag_groups": [
    {"slug": "contexto", "label": "Contexto país", "tags": ["ndc", "nap", "lts", "…los 9 de país"]}
  ],
  "evaluation": {"framework": "", "criteria": []}
}
```

> **Cambio de comportamiento:** hasta ahora cualquier operación que entraba a
> `ido_cci` lanzaba el IET de CAF, fuera de la institución que fuera. Con la
> configuración base, una operación sin institución, o de una institución sin
> configurar, no tiene etapas ni lanza nada de CAF. Una institución que no es
> CAF no puede usar un workflow de CAF (regla de la fase 0).

### Endpoints

**Leer la configuración propia** — cualquier usuario autenticado:

```
GET /api/organizations/me/config/
```

```json
{
  "organization": {"slug": "caf", "name": "CAF", "restricted": true},
  "config": { "…configuración efectiva, forma de arriba…" }
}
```

Sin institución: `"organization": null` y la configuración base. Es lo que el
front tiene que leer en vez de las constantes de `caf-etapas.ts`,
`caf-alineacion.ts`, `caf-portal.ts` y los grupos de etiquetas.

**Leer la de una institución:**

```
GET /api/organizations/{slug}/config/
```

| Quién | Respuesta |
|---|---|
| miembro de esa institución, o superadmin | **200**, misma forma que `/me/config/` |
| cualquier otro (incluido el público) | **404**: no se revela si la institución existe |
| superadmin con un slug que no existe | **404** |

**Reemplazar la configuración** — sólo superadmin. La fase 2 construye la
consola encima de este endpoint:

```
PUT /api/organizations/{slug}/config/
{"config": { "…documento completo…" }}
```

| Caso | Respuesta |
|---|---|
| válido | **200**, misma forma que el `GET`; queda guardada normalizada |
| inválido | **400** `{"config": ["stages[1].launches[0]: la skill «x» no es pública ni de esta institución.", "…"]}`, con todos los errores juntos y la ruta de cada campo |
| no superadmin | **403** |
| institución inexistente | **404** |

Un documento con claves de primer nivel faltantes se completa con las de la
configuración base. Las claves desconocidas se rechazan (400).

`me` queda reservado: no puede ser el slug de una institución (la fase 5 lo
valida al dar de alta).

### Lo que cambia en endpoints existentes

| Endpoint | Cambio |
|---|---|
| `GET /api/document/evidence-tags/` | `group` sale de los `evidence_tag_groups` de la institución del usuario (superadmin con `?organization=`: de esa institución; sin institución: de la base). Mismos valores que hoy para CAF |
| `POST /api/projects/{slug}/stage/` `{"to": ...}` | las etapas válidas son las de la institución de la operación. Una etapa que no existe en su configuración da **400**, igual que hoy una desconocida |
| lanzamientos al entrar a una etapa | salen de `stages[].launches` de la institución de la operación |
| informe editado / guiado / lectura | usan `workflows.report` / `.guided` / `.reading` de la institución. Sin el rol, **400** «no disponible en esta institución» |
| `GET /api/projects/evaluation-records/?organization=` | los criterios de cada registro salen de `evaluation.criteria` de esa institución (mismos 10 para CAF) |
| etiqueta de etapa del documento principal | sale de `stages[].principal_tag` |

### Migración de datos

`user/0008` agrega `Organization.config`. `user/0009`: si existe la institución
`caf`, le guarda la configuración de arriba, con los valores que hoy tiene el
código. Reversible: al volver, le borra la configuración. Sin CAF no hace nada.

El registro de evaluación se arma por las etapas que tienen documento principal
(`stages[].principal_tag`): en CAF, `ido_cci` y `dec`, las mismas de siempre.

### Datos de prueba

`seed_local_instituciones` le pone a `caf` la configuración de CAF y deja a
`otra` con una configuración propia mínima:
- marca «Otra institución»;
- etapas `borrador` → `revision` → `cerrada`;
- `revision` lanza `local-de-otra`;
- sin roles de informe;
- sin criterios.

Así se ve una institución que no es CAF.

| Usuario | `GET /api/organizations/me/config/` | `GET /api/organizations/caf/config/` | `GET /api/organizations/otra/config/` |
|---|---|---|---|
| superadmin | `organization: null` + base | 200 | 200 |
| caf-admin, caf-miembro | `caf` + la de CAF | 200 | 404 |
| otra-miembro | `otra` + la suya | 404 | 200 |
| publico | `organization: null` + base | 404 | 404 |

`PUT .../config/`: 200 sólo para el superadmin; 403 para el resto.

## Fase 2 — Consola del superadmin

*Pendiente.*

## Fase 3 — Portal genérico `/{institución}`

*Pendiente.*

## Fase 4 — Plantilla pública `/app` y portal base sólo superadmin

*Pendiente.*

## Fase 5 — Alta de instituciones

*Pendiente.*
