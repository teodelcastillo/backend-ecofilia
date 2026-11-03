# Contrato de API — plan de instituciones (fases 0 a 5)

Este archivo es **la única fuente** del contrato entre el backend y el frontend
para el plan de instituciones. La sesión del back lo escribe **antes** de
implementar cada fase; la del front programa contra lo que dice acá. Si algo
cambia, se cambia en este archivo, en el mismo PR que lo implementa.

| Fase | Qué | Estado del contrato |
|---|---|---|
| 0 | Aislamiento entre instituciones | **Implementado** (PR #7) |
| 1 | Configuración de cada institución en datos | **Implementado** (PR de `fase-1-backend`) |
| 2 | Consola del superadmin | **Implementado** (PR de `fase-2-backend`) |
| 3 | Portal genérico `/{institución}` | **Implementado** (PR de `fase-3-backend`) |
| 4 | Plantilla pública `/app` y portal base sólo superadmin | **Implementado** (PR de `fase-4-backend`) |
| 5 | Alta de instituciones | **Implementado** (PR de `fase-5-backend`) |

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
- **Si la base local se migró con estas ramas antes del 2 de noviembre de 2026**, hay
  que recrearla una vez (`down -v` y de nuevo `up` + `seed_local_instituciones`).
  Las migraciones de la fase 0 se renumeraron para ir detrás de las que ya están
  en producción. Producción no se ve afectada: nunca aplicó los nombres viejos.

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
| `superadmin@local.test` | superadmin de Ecofilia, **con MFA**: secreto TOTP `JBSWY3DPEHPK3PXP` (el login pide `otp`, porque lo tiene activo; cada código sirve una vez). Así se prueba el camino del MFA aunque hoy no se exija | — |
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
- Etiquetas de operación de CAF → CAF (`document/0025`), y también
  `metodologia-caf` y `salvaguardas-cesas` (`document/0026`). Las de país
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
| un cambio que dejaría a alguien como superadmin: `role=admin` sin institución, por ejemplo sacarle la institución a un admin de institución o darle `admin` a un público | **400**. La consola no crea superadmins; eso se decide en el admin de Django. Para sacar de su institución a un admin, mandá también `"role": "member"` |
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
   copió, la configuración apunta a la copia. La marca lleva el nombre de la
   institución: el de la plantilla es un ejemplo. Se valida para la
   institución nueva, como un `PUT` de la fase 1.
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

Si el email ya es de esa institución, se le actualiza el rol. Si además todavía
no eligió su contraseña (email sin verificar), se le reenvía la invitación
(`invited: true`): es la forma de reinvitar cuando venció el enlace.
Institución inexistente: **404**.

### La invitación

El email lleva un enlace a
`{FRONTEND_BASE_URL}/auth/reset-password?uid=…&token=…&invite=1`, la misma
pantalla de restablecer contraseña. Vale lo que `PASSWORD_RESET_TIMEOUT`
(3 días por defecto). Con `invite=1` el front puede titularla
«Elegí tu contraseña».

Cambio en `POST /api/auth/password/reset/confirm/`: si el email todavía no
estaba verificado, queda verificado. El enlace llegó a ese email, así que la
persona demostró que es suyo. Sin esto, alguien invitado nunca podría entrar.

### Datos de prueba

`seed_local_instituciones` suma una plantilla con las etapas
`inicio` → `analisis` → `cierre`, sin lanzamientos, y una configuración
pública con las etapas `borrador` → `listo`. Así `/app` y el portal base se ven
distintos de CAF y de `otra`.

---

## Plantilla del informe y estilo del portal — pedido del front (pendiente de implementar)

**Estado:** el front ya lo tiene hecho y probado. Mientras el back no lo
implemente, detecta que falta y **no ofrece guardar**: muestra la plantilla
vigente en vista previa y avisa que todavía no se puede guardar. CAF no
necesita nada: sin plantilla propia, el front usa la del IET, idéntica a la de
siempre.

### Para qué

Hasta ahora la plantilla del informe (en CAF, el IET) estaba fija en el código
del front, con los textos y el formato de CAF. Con esto el superadmin arma, por
institución:

- la plantilla de su informe: portada, membrete, secciones y estilo del Word;
- el estilo de su portal: color principal, logo y nombre del encabezado.

### Endpoints

**No hay endpoints nuevos.** Cambia la forma de la configuración en los que ya
existen:

| Endpoint | Cambio |
|---|---|
| `GET /api/organizations/me/config/` y `GET /api/organizations/{slug}/config/` | `config.report_template` **siempre presente**: el objeto guardado o `null`. Así el front sabe que el back lo acepta |
| `PUT /api/organizations/{slug}/config/` | Acepta `report_template` (objeto o `null`) y las tres claves nuevas de `branding` |
| `GET` y `PUT /api/platform/config/{public|template}/` | Lo mismo. Se validan como «sin institución», igual que hoy |
| `POST /api/admin/organizations/` con `from_template: true` | La configuración nueva copia `report_template` y el `branding` nuevo de la plantilla, como copia el resto |

**Permisos:** los de la configuración, sin cambios.

- **Escribir:** sólo el superadmin; un miembro recibe 403.
- **Leer:** el miembro y el superadmin (quien no es miembro recibe 404).

La plantilla no tiene datos de operaciones.

**Versión:** sigue siendo `1`, porque todo lo nuevo es opcional. Una
configuración sin estas claves sigue siendo válida.

### `branding`: tres claves opcionales

| Clave | Tipo | Regla | Sin valor |
|---|---|---|---|
| `portal_title` | string | se guarda recortado; vacío = sin valor | el front usa `display_name` («Asistente Virtual DATBC» con el marco `caf-datbc`) |
| `primary_color` | string | hexadecimal de 6 dígitos sin `#` (`1D4ED8`); se guarda en mayúsculas | el color de la plataforma |
| `portal_logo_url` | string | ruta del front (`/…`) o URL `https://` | la hoja de Ecofilia |

El front no manda las claves vacías. El back **no las devuelve** si no tienen valor.

### `report_template`

`null` (o ausente al escribir) = sin plantilla propia.

```json
{
  "title": "INFORME DE EVALUACIÓN TÉCNICA DATBC",
  "subtitle": "Inventario de Operaciones (IDO)",
  "file_prefix": "IET",
  "assistant_name": "IET DATBC — Evaluación Técnica de la Operación",
  "letterhead": {
    "logo_url": "/images/caf-logo.png",
    "logo_width": 180,
    "logo_height": 31,
    "lines": ["Informe de Evaluación Técnica DATBC", "Etapa de Inventario de Operaciones (IDO)"]
  },
  "cover": {
    "intro": "…",
    "structure_lead": "El informe comprende las siguientes secciones:",
    "structure": ["Sección A: Descripción de la operación", "…"],
    "disclaimer": "…"
  },
  "sections": [
    {
      "key": "A", "heading": "SECCIÓN A. DESCRIPCIÓN DE LA OPERACIÓN", "intro": "",
      "source": "operacion", "include_empty_fields": true, "include_objective": true,
      "conclusion": null, "groups": [], "inserts": [], "table_layout": "mitades",
      "closing_heading": "", "closing": ""
    },
    {
      "key": "D", "heading": "SECCIÓN D. …", "intro": "…", "source": "analisis",
      "include_empty_fields": false, "include_objective": true, "conclusion": null,
      "groups": [{"number": "1", "heading": "D.1. Enfoque de Transacción — Objetivo 1: …"}],
      "inserts": [], "table_layout": "ultima_ancha", "closing_heading": "", "closing": ""
    },
    {
      "key": "E", "heading": "SECCIÓN E. …", "intro": "", "source": "analisis",
      "include_empty_fields": false, "include_objective": true, "conclusion": "criterios",
      "groups": [], "inserts": [], "table_layout": null,
      "closing_heading": "E.4. Registro y Trazabilidad del Informe",
      "closing": "**Participación de la DATBC:** … Link a FOCUS: {{enlace:link_focus}}"
    }
  ],
  "style": {
    "body_font": "Aptos", "display_font": "Aptos Display",
    "text_color": "000000", "heading_color": "2E74B5", "subheading_color": "005288",
    "accent_color": "13B67E", "table_header_color": "E7F5EF", "table_border_color": "D8E5E0"
  }
}
```

| Campo | Tipo | Reglas |
|---|---|---|
| `title` | string | no vacío |
| `subtitle`, `file_prefix`, `assistant_name` | string | pueden ser `""` |
| `letterhead.logo_url` | string | `""`, ruta del front (`/…`), URL `https://` o `data:image/png;base64,…` / `data:image/jpeg;base64,…` |
| `letterhead.logo_width` | número | `0 < x ≤ 600` |
| `letterhead.logo_height` | número | `0 < x ≤ 200` |
| `letterhead.lines` | lista de string | se descartan las vacías |
| `cover.intro`, `cover.structure_lead`, `cover.disclaimer` | string | pueden ser `""`; `intro` es Markdown |
| `cover.structure` | lista de string | se descartan las vacías; vacía = el front usa los `heading` |
| `sections` | lista | **al menos una**; a lo sumo **una** con `source: "operacion"` |
| `sections[].key` | string | `^[A-Z]{1,3}$` o `"*"`; única; **`"S"` reservada** (pasos internos del asistente). `"*"` sólo con `source: "analisis"` |
| `sections[].heading` | string | no vacío |
| `sections[].intro` | string | Markdown, puede ser `""` |
| `sections[].source` | string | `"operacion"` o `"analisis"` |
| `sections[].include_empty_fields`, `include_objective` | bool | default `false` y `true` |
| `sections[].conclusion` | string \| null | `"criterios"`, `"componentes"` o `null` |
| `sections[].groups[]` | `{number, heading}` | `number` dígitos, único en la sección; `heading` no vacío |
| `sections[].inserts[]` | `{after_step, heading, instruction}` | `after_step` entero ≥ 1 (no bool); `heading` no vacío |
| `sections[].table_layout` | string \| null | `"mitades"`, `"ultima_ancha"` o `null` |
| `sections[].closing_heading`, `closing` | string | pueden ser `""`; `closing` es Markdown con variables `{{operacion}}`, `{{nota:clave}}`, `{{enlace:clave}}` (el back no las interpreta) |
| `style.*_font` | string | no vacío |
| `style.*_color` | string | hexadecimal de 6 dígitos sin `#`; se guarda en mayúsculas |

Una clave desconocida en `report_template`, en una sección o en `branding` es error.

**El back guarda la plantilla, no la interpreta.** El armado del informe y del
Word es del front:

- cada paso del asistente cae en la sección cuya `key` trae al principio del título («B.1. …» va a la B);
- `*` se lleva los que no reclama otra sección;
- los pasos «S.n.» nunca van al informe.

### Errores

Igual que el resto de la configuración: **400** con todos los errores juntos y la ruta de cada uno.

```json
{"config": [
  "report_template.title: no puede estar vacío.",
  "report_template.sections[1].key: «S» está reservada para los pasos internos del asistente.",
  "report_template.sections[2].key: «B» está repetida.",
  "report_template.style.heading_color: un color hexadecimal de seis dígitos, sin «#».",
  "branding.primary_color: un color hexadecimal de seis dígitos, sin «#»."
]}
```

**Mensajes:** los del front, para que la consola los muestre igual que los suyos.

| Ruta | Mensaje |
|---|---|
| `report_template` | `tiene que ser un objeto o null.` |
| `report_template.sections` | `tiene que haber al menos una sección.` · `puede haber una sola sección con los datos de la operación.` |
| `….key` | `una a tres mayúsculas (A, B, IA) o «*».` · `«S» está reservada para los pasos internos del asistente.` · `«X» está repetida.` |
| `report_template.sections[i]` | `la sección «*» tiene que ser de análisis.` |
| `….heading`, `report_template.title`, `….groups[j].heading`, `….inserts[j].heading`, `style.*_font` | `no puede estar vacío.` |
| `….groups[j].number` | `tiene que ser un número.` · `«n» está repetido.` |
| `….inserts[j].after_step` | `tiene que ser un número de paso (1 o más).` |
| `….source` / `….conclusion` / `….table_layout` | `tiene que ser operacion o analisis.` / `criterios, componentes o null.` / `mitades, ultima_ancha o null.` |
| `style.*_color`, `branding.primary_color` | `un color hexadecimal de seis dígitos, sin «#».` |
| `report_template.letterhead.logo_url` | `una URL https, una ruta del sitio o una imagen PNG/JPEG.` |
| `report_template.letterhead` | `el logo mide hasta 600 × 200 píxeles.` |
| `branding.portal_logo_url` | `tiene que ser una ruta del front (/...) o una URL https.` |

**Implementación de referencia:** la validación en Python que espeja la del
front está en el repo del front, en `docs/referencia-back-plantilla-informe.py`.
Expone `report_template(value, errors)` y `branding_opcionales(value, errors)`.
Engancharla en `apps/user/institution_config.py`:

- `"report_template": None` en `BASE_CONFIG`, así queda en `TOP_LEVEL_KEYS` y el GET la devuelve siempre;
- en `validate_config`: `config["report_template"] = report_template(config.get("report_template"), errors)`;
- en `_branding`: aceptar las tres claves y sumar `**branding_opcionales(value, errors)` al resultado.

**Auditoría:** el cambio queda en el `config.organization.updated` /
`config.platform.updated` que ya se registra.

**Migración:** ninguna. CAF y las instituciones existentes quedan con
`report_template: null`, y el front usa la plantilla del asistente del IET,
idéntica a la de antes.

### Cómo se comprueba

- `pnpm test:contrato` en el front: «API › la configuración guarda y devuelve el estilo y la plantilla» deja de saltearse en cuanto el GET devuelve `report_template`.
- Esa prueba verifica:
  - la ida y vuelta de la configuración;
  - que el miembro la lee;
  - el 400 con «S» reservada;
  - el 403 a un miembro que intenta escribirla.

## Seguridad — lo que cambia para el front

Detalle y evidencia en `backend/docs/seguridad.md`.

| Cambio | Qué tiene que hacer el front |
|---|---|
| La API acepta **sólo** `Authorization: Bearer` (sin Basic ni sesión) | Nada: ya manda Bearer |
| **MFA obligatorio para superadmin y staff — implementado y APAGADO por ahora** (`ADMIN_REQUIRE_MFA`). Mientras esté apagado, `mfa_required` es siempre `false` y no hay 403 por MFA; el MFA es opcional para todos y quien lo activa lo usa en el login. Prendido: sin MFA, la API los trata como usuarios comunes, y `/api/auth/me/` y el `user` del login traen `mfa_required: true`, `is_ecofilia_admin: false` y `home_path: "/app"` | Con `mfa_required`, mostrar la activación de MFA (`POST /api/auth/mfa/setup/` → mostrar el QR de `otpauth_url` → `POST /api/auth/mfa/verify/` con el código) y pedir un login nuevo |
| Acciones de superadmin sin MFA: **403** con un mensaje que nombra el MFA | Llevar a la activación de MFA |
| **Bloqueo** tras 10 fallos de login (contraseña o código), por 15 minutos. El error es el mismo que una contraseña equivocada | Nada. Opcional: decir «si fallaste varias veces, esperá unos minutos» |
| Un **código MFA sirve una sola vez** | Si el login con código falla justo después de otro, pedir el código siguiente |
| `POST /api/auth/mfa/setup/` con MFA activo: **400** | Para reconfigurar, primero `POST /api/auth/mfa/disable/` con un código |
| **Cambiar o restablecer la contraseña invalida los tokens**, también el actual | Después de cambiar la contraseña, volver al login |
| **Al desplegar, todos vuelven a iniciar sesión una vez** | Nada: el refresh falla y el front ya manda al login |
| Publicar una evaluación (`visibility: "public"`) es del superadmin: **403** para el resto | Ocultar «pública» para quien no es superadmin |
| Lo que un miembro marcó público antes sólo lo ve su institución | Nada |
| Quien sale de una institución deja de ver lo que creó ahí: **404** | Nada |
| Una institución con miembros u operaciones no se puede borrar | La consola no ofrece borrar; dar de baja a los miembros primero |
| `GET /api/admin/audit/?action=&organization=&actor=&since=&limit=` (superadmin con MFA) | Pantalla de auditoría en la consola. Cada evento trae `created_at`, `actor_email`, `action`, `organization_slug`, `target_type`, `target_id`, `data` e `ip` |
| Crear evaluaciones o documentos sobre una operación ajena responde «no encontrado», igual que si no existiera | Nada |

### Revisión del front (2 de noviembre de 2026)

| Cambio | Qué tiene que hacer el front |
|---|---|
| **El registro responde lo mismo exista o no el email**: `201` con `{"detail": "Revisa tu correo para confirmar tu cuenta."}` y **sin `user`**. A quien ya tenía cuenta le llega «Ya tenés una cuenta en Ecofilia» con el enlace de restablecer (`/auth/reset-password?uid=&token=`). Una contraseña débil sigue dando `400` con `password`, en los dos casos | No leer `user` de la respuesta del registro, ni mostrar «ese email ya existe»: siempre «revisá tu correo» |
| **Restablecer la contraseña ya no apaga el MFA** | Después del restablecer, el login sigue pidiendo el código si la cuenta tenía MFA. Quien perdió su app de MFA: «pedile a un administrador de Ecofilia que te lo reinicie» |
| **`is_staff` solo no da alcance global.** Un staff que no es superadmin es, para la API, un usuario público: `is_ecofilia_admin: false`, `home_path: "/app"` | Nada: el front ya decide por `is_ecofilia_admin` y `home_path` |
| El secreto MFA se guarda cifrado | Nada: `setup` devuelve el secreto y el `otpauth_url` como antes |
