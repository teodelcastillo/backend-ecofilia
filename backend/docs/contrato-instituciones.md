# Contrato de API — plan de instituciones (fases 0 a 5)

Este archivo es **la única fuente** del contrato entre el backend y el frontend
para el plan de instituciones. La sesión del back lo escribe **antes** de
implementar cada fase; la del front programa contra lo que dice acá. Si algo
cambia, se cambia en este archivo, en el mismo PR que lo implementa.

| Fase | Qué | Estado del contrato |
|---|---|---|
| 0 | Aislamiento entre instituciones | **Implementado** (PR #7) |
| 1 | Configuración de cada institución en datos | Pendiente |
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

*Pendiente. Se escribe antes de implementarla.*

## Fase 2 — Consola del superadmin

*Pendiente.*

## Fase 3 — Portal genérico `/{institución}`

*Pendiente.*

## Fase 4 — Plantilla pública `/app` y portal base sólo superadmin

*Pendiente.*

## Fase 5 — Alta de instituciones

*Pendiente.*
