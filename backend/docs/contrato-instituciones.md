# Contrato de API — plan de instituciones (fases 0 a 5)

Este archivo es **la única fuente** del contrato entre el backend y el frontend
para el plan de instituciones. La sesión del back lo escribe **antes** de
implementar cada fase; la del front programa contra lo que dice acá. Si algo
cambia, se cambia en este archivo, en el mismo PR que lo implementa.

| Fase | Qué | Estado del contrato |
|---|---|---|
| 0 | Aislamiento entre instituciones | **Implementado** (PR #7) |
| 1 | Configuración de cada institución en datos | **Implementado** (PR de `fase-1-backend`) |
| 2 | Consola del superadmin | **Escrito**, en implementación (`fase-2-backend`) |
| 3 | Portal genérico `/{institución}` | **Escrito**, en implementación (`fase-3-backend`) |
| 4 | Plantilla pública `/app` y portal base sólo superadmin | **Escrito**, en implementación (`fase-4-backend`) |
| 5 | Alta de instituciones | **Escrito**, en implementación (`fase-5-backend`) |

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

## Decisiones que tomó el back y hay que confirmar (fases 2 a 5)

1. **Copias de skills al dar de alta (fase 5).** Una institución nueva puede
   arrancar con copias propias de skills de **Ecofilia** (alcance `public` o
   `platform`). **No** puede copiar skills de otra institución: copiar un
   workflow de CAF sería darle a otra institución información de CAF. Si CAF
   acepta compartir uno, el superadmin lo pasa primero a `platform`.
2. **Dos configuraciones de plataforma (fase 4).** La **pública** rige `/app`:
   usuarios sin institución y operaciones sin institución. La **plantilla** es
   el «portal base», sólo para el superadmin, y es de donde sale la
   configuración de una institución nueva.
3. **A dónde va cada uno al entrar (fase 3).** Lo dice el back
   (`home_path` en `/api/auth/me/`):
   - el superadmin, a `/admin`;
   - un miembro, a `/{slug de su institución}`;
   - el público, a `/app`.

---

## Fase 2 — Consola del superadmin

Todo lo de `/api/admin/` es **sólo del superadmin**
(`is_ecofilia_admin`); cualquier otro recibe **403**.

### Resumen

```
GET /api/admin/overview/
```

```json
{
  "levels": {"superadmin": 1, "organization": 3, "public": 1},
  "skills_by_scope": {"platform": 12, "public": 1, "organization": 6, "user": 1},
  "organizations": [ { "…item de institución, ver abajo…" } ]
}
```

### Instituciones

```
GET   /api/admin/organizations/
GET   /api/admin/organizations/{slug}/
PATCH /api/admin/organizations/{slug}/
```

Cada item del listado:

```json
{
  "slug": "caf",
  "name": "CAF",
  "restricted": true,
  "has_config": true,
  "members_count": 2,
  "operations_count": 1,
  "own_skills_count": 5,
  "own_tags_count": 11,
  "created_at": "2026-10-29T17:00:00Z"
}
```

El detalle suma:

```json
{
  "enabled_features": ["projects", "library", "chat"],
  "enabled_skill_slugs": ["caf-iet-datbc-evaluacion-tecnica"],
  "default_skill_slugs": ["caf-iet-datbc-evaluacion-tecnica"]
}
```

`PATCH` acepta cualquiera de estos campos: `name`, `restricted`,
`enabled_features`, `enabled_skill_slugs` y `default_skill_slugs`.

| Caso | Respuesta |
|---|---|
| válido | **200** con el detalle |
| `enabled_features` con una sección desconocida | **400**. Las conocidas: `projects`, `library`, `repositories`, `chat`, `skills`, `evaluations`, `tableros`, `inventories` |
| una skill de `enabled_skill_slugs` / `default_skill_slugs` que no es `public` ni de esta institución | **400** |
| `default_skill_slugs` con una que no está en `enabled_skill_slugs` | **400** |
| `name` repetido | **400** |
| institución inexistente | **404** |

El `slug` no se cambia: está en las URLs del portal (`/{slug}`). La
configuración se edita con `PUT /api/organizations/{slug}/config/` (fase 1).

### Usuarios

```
GET   /api/admin/users/?level=superadmin|organization|public&organization=<slug>&q=<texto>
PATCH /api/admin/users/{id}/
```

Todos los filtros son opcionales. `q` busca en email, nombre y apellido.
Orden: por email.

```json
{
  "id": 7,
  "email": "m@caf.org",
  "first_name": "",
  "last_name": "",
  "level": "organization",
  "organization_slug": "caf",
  "role": "member",
  "is_active": true,
  "email_verified": true,
  "last_login": "2026-10-29T17:00:00Z",
  "date_joined": "2026-10-01T12:00:00Z",
  "operations_count": 1
}
```

`level` ∈ `superadmin` · `organization` · `public` (la misma regla que
`listar_usuarios_por_nivel`).

`PATCH` acepta `organization_slug` (slug o `null`), `role`
(`member` · `manager` · `admin`) e `is_active`.

| Caso | Respuesta |
|---|---|
| válido | **200** con el usuario |
| darle institución a un usuario `is_staff` o `is_superuser` | **400**: tendría acceso a todas las instituciones |
| el superadmin cambiándose a sí mismo `organization_slug`, `role` o `is_active` | **400**: no puede quitarse el acceso por error |
| institución inexistente | **400** |
| usuario inexistente | **404** |

Cambiar a alguien de institución **no mueve sus operaciones**: siguen siendo de
la institución en la que se crearon.

### Skills por alcance

- `GET /api/skills/` acepta `?scope=<alcance>` y `?organization=<slug>` (las
  de alcance institución de esa institución). Se aplican sobre lo que cada uno
  ve; para el superadmin, sobre todas.
- Cambio de alcance en lote (lo mismo que `listar_skills_alcance --hacer-publicas`):

```
POST /api/admin/skills/scope/
{"slugs": ["ecofilia-qa-brief", "ecofilia-document-summary"], "scope": "public"}
{"slugs": ["x"], "scope": "organization", "organization_slug": "caf"}
```

| Caso | Respuesta |
|---|---|
| válido | **200** `[{"slug", "scope", "organization_slug"}]` |
| algún slug inexistente | **400** con los que faltan; no cambia ninguno |
| `scope="organization"` sin `organization_slug`, o con uno inexistente | **400** |

### Etiquetas

El superadmin puede mover una etiqueta de institución:
`PATCH /api/document/evidence-tags/{slug}/?organization=<de dónde>` con
`{"organization_slug": "<a dónde>" | null}`. Si el slug ya existe dentro de lo
que ve la institución de destino, **400**. El resto de la edición de
etiquetas no cambia.

---

## Fase 3 — Portal genérico `/{institución}`

El portal `/caf` pasa a ser `/{slug}`: el mismo portal para cualquier
institución, armado con su configuración (fase 1). Del lado del back hacen
falta tres cosas: decir a dónde va cada uno, listar los portales que puede
abrir y que ningún endpoint asuma CAF.

### A dónde va cada uno

`GET /api/auth/me/` (y el `user` del login) suma `home_path`:

| Quién | `home_path` |
|---|---|
| superadmin | `"/admin"` |
| miembro de una institución | `"/{slug}"` (p. ej. `"/caf"`) |
| público | `"/app"` |

### Qué portales puede abrir

```
GET /api/organizations/
```

```json
[{"slug": "caf", "name": "CAF", "restricted": true, "display_name": "CAF", "logo_url": "/images/caf-logo.png"}]
```

| Quién | Recibe |
|---|---|
| superadmin | todas |
| miembro | sólo la suya |
| público | `[]` |

Entrar a `/{slug}` es poder leer `GET /api/organizations/{slug}/config/`: si da
404, ese usuario no tiene ese portal.

### Endpoints que ya no asumen CAF

| Endpoint | Antes | Ahora |
|---|---|---|
| `POST /api/projects/historical-report/` | sin `organization` usaba `caf` | `organization` es obligatorio: **400** sin él |
| `analysis_running` del listado de operaciones | miraba sólo el IET de CAF | mira el workflow `report` de la configuración de la institución de cada operación |

Los listados de un portal ya se piden con `?organization=<slug>`. Son
operaciones, informes emitidos, registro de evaluación e historial de
alineación, y siguen igual.

---

## Fase 4 — Plantilla pública `/app` y portal base sólo superadmin

Dos configuraciones de plataforma, con la misma forma que la de una institución
(fase 1).

| Configuración | Para qué | Quién la lee | Quién la escribe |
|---|---|---|---|
| **pública** | `/app`: usuarios sin institución y operaciones sin institución | cualquier usuario autenticado | superadmin |
| **plantilla** | portal base: punto de partida de una institución nueva (fase 5) | sólo superadmin | superadmin |

```
GET /api/platform/config/public/        → {"config": {...}}
PUT /api/platform/config/public/        {"config": {...}}
GET /api/platform/config/template/      → {"config": {...}}
PUT /api/platform/config/template/      {"config": {...}}
```

| Caso | Respuesta |
|---|---|
| `GET public` | **200** para cualquiera autenticado |
| `GET template` sin ser superadmin | **403** |
| `PUT` sin ser superadmin | **403** |
| `PUT` inválido | **400** `{"config": [...]}`. Las dos se validan como «sin institución»: sólo skills `public` y etiquetas globales |
| `PUT` válido | **200** con la configuración guardada |

- Sin configuración pública guardada rige la base de la fase 1. La pública es
  la que devuelve `GET /api/organizations/me/config/` a un usuario sin
  institución.
- Sin plantilla guardada, la plantilla es la base de la fase 1.
- El «portal base» del front es el portal genérico de la fase 3, armado con la
  plantilla: sirve para que el superadmin vea cómo arranca una institución
  nueva antes de darla de alta.

---

## Fase 5 — Alta de instituciones

```
POST /api/admin/organizations/
```

```json
{
  "name": "Banco del Sur",
  "slug": "banco-del-sur",
  "restricted": false,
  "enabled_features": ["projects", "library", "chat"],
  "from_template": true,
  "clone_skills": ["ecofilia-qa-brief"],
  "members": [{"email": "ana@bancodelsur.org", "role": "admin"}]
}
```

Sólo `name` y `slug` son obligatorios. Por defecto: `restricted=false`,
`enabled_features=[]`, `from_template=true`, `clone_skills=[]`, `members=[]`.

Qué hace, todo o nada (si algo falla, no se crea nada):

1. **Crea la institución.**
2. **Copia las skills de `clone_skills`.** Tienen que ser `public` o
   `platform`. Cada copia:
   - es de la institución nueva (`scope="organization"`) y se llama
     `{slug}-{slug original}`;
   - lleva sus pasos y parámetros;
   - queda en `enabled_skills`, y en `default_project_skills` si corre sobre
     operaciones.
3. **Arma la configuración.** Con `from_template`, es la plantilla (fase 4); si
   no, queda vacía y rige la base. Si la plantilla nombra una skill que se
   copió, la configuración apunta a la copia. Se valida para la institución
   nueva, como un `PUT` de la fase 1.
4. **Agrega a los miembros.**
   - Un email que no existe crea el usuario y le manda una invitación. El
     usuario queda aprobado, sin contraseña y con el email sin verificar.
   - Un usuario existente sin institución pasa a la nueva.
   - El email llega cuando se confirma el alta.

| Caso | Respuesta |
|---|---|
| válido | **201**: el detalle de la fase 2 más `"cloned_skills": [{"from", "slug"}]` y `"members": [{"email", "id", "created", "invited"}]` |
| `slug` con formato inválido, repetido o reservado | **400** |
| `name` repetido | **400** |
| una skill de `clone_skills` inexistente o de una institución | **400** |
| la configuración resultante inválida | **400** `{"config": [...]}` |
| un miembro que ya es de otra institución, o que es `is_staff` / `is_superuser` | **400** |
| no superadmin | **403** |

**Slugs reservados** (chocan con rutas del front o de la API): `admin`, `api`,
`app`, `auth`, `base`, `demo`, `evaluations`, `forgot-password`, `home`,
`library`, `login`, `me`, `my-library`, `platform`, `projects`,
`reset-password`, `signup`.

### Sumar miembros a una institución existente

```
POST /api/admin/organizations/{slug}/members/
{"email": "juan@bancodelsur.org", "role": "member"}
```

Mismas reglas que `members` en el alta. **201** con
`{"email", "id", "created", "invited"}`.

### La invitación

El email lleva un enlace a
`{FRONTEND_BASE_URL}/auth/reset-password?uid=…&token=…&invite=1`, la misma
pantalla de restablecer contraseña. Con `invite=1` el front puede titularla
«Elegí tu contraseña».

Cambio en `POST /api/auth/password/reset/confirm/`: si el email todavía no
estaba verificado, queda verificado. El enlace llegó a ese email, así que la
persona demostró que es suyo. Sin esto, alguien invitado nunca podría entrar.

### Datos de prueba

`seed_local_instituciones` suma una plantilla con las etapas
`inicio` → `analisis` → `cierre`, sin lanzamientos, y una configuración
pública con las etapas `borrador` → `listo`. Así `/app` y el portal base se ven
distintos de CAF y de `otra`.
