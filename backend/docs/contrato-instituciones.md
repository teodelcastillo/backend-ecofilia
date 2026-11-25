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
| — | Configuración completa desde la consola | **Implementado** (25 de noviembre de 2026, sección al final) |

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
| Operación | `local-caf-ido` | de `caf-miembro`, institución `caf`, etapa IDO, con su informe de Ecofilia (ver «Prueba local del flujo CAF») |
| Operación | `local-caf-dec` | de `caf-miembro`, institución `caf`, etapa DEC, con el informe de IDO y el de DEC |
| Documento | `doc-caf`, `doc-otra`, `doc-publico` | uno por operación |
| Documento | `doc-local-caf-ido` (`ido`), `doc-local-caf-dec-ido` (`ido`), `doc-local-caf-dec` (`dec`) | de las operaciones de etapa |
| Informe emitido | uno por operación de `local-doc-*` | etapa `ido_cci` |
| Corrida | una en `local-doc-otra` | skill `local-de-otra` |
| Corrida | completadas, inventadas, en `local-caf-ido` y `local-caf-dec` | IET de IDO y de DEC (`metadata.seed`) |
| Skill | `local-plataforma` | `platform` |
| Skill | `local-publica` | `public` |
| Skill | `local-de-otra` | `organization` / `otra` |
| Skill | `caf-*` (de las migraciones) | `organization` / `caf`; habilitadas para CAF salvo `caf-insumo-ejecutivo` |
| Skill | `caf-iet-datbc-evaluacion-tecnica`, `caf-iet-guiado-ejecutivo`, `dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion` | `organization` / `caf`; **pisadas con la definición de producción** (ver «Prueba local del flujo CAF») |
| Skill | `local-propia-del-publico` | `user` / dueño `publico` |
| Etiqueta | `propia-de-otra` | institución `otra` |
| Etiqueta | `ido`, `dec`, `ias`, `ibat`, …, `metodologia-caf`, `salvaguardas-cesas`, `mision-evaluacion` | institución `caf` (12) |
| Etiqueta | `ndc`, `nap`, `lts`, … | globales (las 9 de país) |

**Lo que tiene que ver cada uno** (verificado con `verificar_contrato_local.py`):

| Usuario | `GET /api/skills/` | Operaciones y emitidos | Etiquetas con institución | `evaluation-records` sin org / `caf` / `otra` | `GET /api/skills/local-plataforma/` |
|---|---|---|---|---|---|
| superadmin | todas | las 5 | las de `caf` y `otra` | 400 / 200 / 200 | 200 |
| caf-admin, caf-miembro | 4 `caf-*` + la IET de DEC + `local-publica` | las 3 de CAF | las 12 de `caf` | 400 / 200 / 403 | 404 |
| otra-miembro | `local-de-otra` + `local-publica` | la suya | `propia-de-otra` | 400 / 403 / 200 | 404 |
| publico | `local-publica` + `local-propia-del-publico` | la suya | ninguna | 400 / 403 / 403 | 404 |

Todos ven las 9 etiquetas globales (las de país). Ninguna etiqueta de CAF le
llega a `otra` ni al público.

### Prueba local del flujo CAF (IDO y DEC)

Con el seed, la base local tiene el ciclo de CAF como está **hoy en
producción**, incluido DEC, y una operación lista para mirar en cada etapa. No
hace falta ninguna clave de LLM: las corridas ya están completadas.

**Qué siembra además de lo anterior**

- **Los workflows de CAF como están en producción**, desde
  `apps/user/seed_local/workflows/*.json`: la IET de IDO
  (`caf-iet-datbc-evaluacion-tecnica`, con «D.1.1. … (CT A1)», E.1/E.2/E.3,
  S.1/S.2), la IET de DEC
  (`dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion`, que no siembra
  ninguna migración) y el guiado (`caf-iet-guiado-ejecutivo`, cuyos títulos son
  las secciones del ejecutivo). En producción el staff los ajusta en el Studio y
  las migraciones ya no los describen: el seed los **pisa** con esa copia. Los
  pasos se actualizan por posición, así que sus ids no cambian al re-sembrar.
- Con la IET de DEC presente, vuelve a aplicar `user/0016`: la etapa DEC tiene
  su informe (`stages[].report`) y lo lanza. También `document/0027` y
  `skill/0052` (Misión de Evaluación).
- **`local-caf-ido`** (etapa `ido_cci`, «Agua y energía resiliente en Valle
  Azul») y **`local-caf-dec`** (etapa `dec`, «Riego y energía solar en Río
  Claro»), de `caf-miembro`, con su documento principal etiquetado, el
  recorrido de etapas y una corrida **completada** del informe de la etapa (la
  de DEC tiene también la de IDO). El contenido es inventado y tiene la
  estructura de producción: C.1 (B.1 en DEC) con una tabla Campo | Detalle por
  actividad y las tres variantes del rótulo de la actividad («Actividad»,
  «Actividad elegible (nivel 4)», «Actividad de financiamiento elegible»); cada
  CT con su prosa y el cierre `**Conclusión:**` / `**Información faltante:**`;
  las recomendaciones (E.3 / D.1) con subtítulos `E.3.1`–`E.3.4` y viñetas; S.1
  y S.2 como tablas (la IET de DEC no los tiene, igual que en producción). Al
  re-sembrar, esas corridas se reescriben en el lugar: un informe del ejecutivo
  ya empezado sobre ellas no se pierde.

**Levantar de cero**

```bash
cd backend
docker compose -f docker-compose.dev.yml down -v      # borra la base local
docker compose -f docker-compose.dev.yml up --build   # migra sola al arrancar
docker exec -it ecofilia-backend-dev ./manage.py seed_local_instituciones
```

Para no tocar la base dev de todos los días, se puede usar una base aparte en el
mismo Postgres y un segundo backend en el puerto 8001:

```bash
docker exec ecofilia-db-dev psql -U postgres -c "CREATE DATABASE ecofilia_seed"
docker exec ecofilia-db-dev psql -U postgres -d ecofilia_seed -c "CREATE EXTENSION IF NOT EXISTS vector"
docker exec -e POSTGRES_NAME=ecofilia_seed ecofilia-backend-dev ./manage.py migrate
docker exec -e POSTGRES_NAME=ecofilia_seed ecofilia-backend-dev ./manage.py seed_local_instituciones
# Backend sobre esa base, en :8001, con el mismo entorno que el dev:
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' ecofilia-backend-dev \
  | grep -v '^POSTGRES_NAME=' | grep -v '^$' | { cat; echo POSTGRES_NAME=ecofilia_seed; } \
  | docker run -d --name ecofilia-backend-seed --network backend_app-network \
      -v "$PWD":/code -w /code -p 8001:80 --env-file /dev/stdin \
      ecofilia-backend:dev ./manage.py runserver 0:80
# Front contra ese backend (en ecofiliaV2):
NEXT_PUBLIC_API_URL=http://localhost:8001/api pnpm exec next dev
```

Para tirarla: `docker rm -f ecofilia-backend-seed` y
`docker exec ecofilia-db-dev psql -U postgres -c "DROP DATABASE ecofilia_seed"`.

**Entrar.** Mientras el bloqueo de DEC siga activo en el front
(`ecofiliaV2/lib/dec-bloqueado.ts`), **DEC sólo lo ve el superadmin**:
`caf-miembro` ve `local-caf-ido` y no `local-caf-dec`. El superadmin pide
código TOTP al iniciar sesión (`pyotp.TOTP("JBSWY3DPEHPK3PXP").now()`, cada
código sirve una vez). Para no pasar por el login, un JWT de la base local:

```bash
docker exec -i ecofilia-backend-dev ./manage.py shell -v 0 -c "from rest_framework_simplejwt.tokens import RefreshToken; from django.contrib.auth import get_user_model; r = RefreshToken.for_user(get_user_model().objects.get(email='superadmin@local.test')); print(r.access_token); print(r)"
```

y en la consola del navegador, sobre `localhost:3000`:
`localStorage.setItem("access_token", "<access>")` y
`localStorage.setItem("refresh_token", "<refresh>")`. **Volver a correr el seed
cambia las claves**, y con eso invalida las sesiones abiertas: hay que volver a
entrar.

**Qué mirar** (en `localhost:3000/caf/operaciones/<slug>`):

| Operación | Insumo IA | Informe IET | Guiado («Empezar informe») |
|---|---|---|---|
| `local-caf-ido` | 3 actividades (A.1.1.6, M.8.1.1, B.2.1.6); Obj. 1 «Con brechas por CT A2», Obj. 2 «Alineada en el CT M2»; falta información en A1, A2 y M4 | el informe IA de IDO, con las secciones del ejecutivo en blanco | 16 pasos; recomendaciones precargadas desde E.3.1–E.3.4 |
| `local-caf-dec` | 3 actividades (M.1.1.1, A.1.1.2, B.2.1.8), leídas de B.1; Obj. 1 «Alineada», Obj. 2 «Con brechas» | el informe IA de DEC | 15 pasos; componentes verdes desde B.1 |

**Refrescar los workflows de producción.** Cuando el staff cambie una de las
tres en el Studio, se baja su definición (sólo lectura, con una sesión de
superadmin en producción: `GET https://api.ecofilia.site/api/skills/<slug>/`),
se guarda la respuesta como `respuesta.json` y se limpia (sin dueño ni ids):

```bash
docker exec -i ecofilia-backend-dev ./manage.py shell -v 0 -c "import json, sys; from apps.user.seed_local.workflows import limpiar; json.dump(limpiar(json.load(sys.stdin)), sys.stdout, ensure_ascii=False, indent=2); print()" \
  < respuesta.json > apps/user/seed_local/workflows/<slug>.json
```

Copia tomada de producción el 17 de noviembre de 2026. Antes de commitear una
copia nueva, revisar que no traiga datos de clientes (`document_slugs`,
`pinned_document_slugs`, ejemplos en las instrucciones).

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
| `stages[].report` | slug de skill, opcional | el informe de Ecofilia **de esa etapa** (en CAF, DEC tiene el suyo). Se suma a `launches` si no estaba. Sin él, vale `workflows.report` si la etapa lo lanza |
| `workflows.report` / `.guided` / `.reading` | slug de skill \| null | ídem; `null` = la institución no tiene ese rol |
| `evidence_tag_groups[]` | lista | `slug` único; una etiqueta en a lo sumo un grupo; cada etiqueta existe para la institución. Una etiqueta sin grupo cae en `"otro"` |
| `evaluation.framework` | string | `"caf-datbc"` o `""`. Con `caf-datbc`, los criterios tienen que ser exactamente los 10 de CAF: las reglas de determinación están en código y dependen de esas claves |
| `evaluation.criteria[]` | lista | `key` única; `group` es la clave del criterio-determinación al que pertenece (o `null`); `options[].tone` ∈ `ok`, `atencion`, `brecha`, `critico`, `pendiente`, `neutro` |

Una etapa tiene **informe de Ecofilia** si tiene `stages[].report` o si sus
`launches` incluyen `workflows.report`. En CAF, `ido_cci` (la IET) y, desde
`user/0016`, `dec` (`dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion`).
Ahí se ofrecen las vías editado y guiado; el guiado (`workflows.guided`) es el
mismo para todas las etapas y reescribe el informe de la etapa en la que está la
operación. Lo que reescribe se publica directo en el informe del ejecutivo, sin
tope de generaciones.

**Avanzar de etapa** (`POST /api/projects/{slug}/stage/`): cada etapa con
informe por la que pasó la operación, anterior a la de destino, tiene que tener
el informe del ejecutivo emitido; si no, **400** con cuáles faltan. Una
operación creada de entrada en DEC no pasó por IDO y no se le pide.

**El informe de una etapa analiza su documento principal**: si el principal
lleva la etiqueta de otra etapa (en DEC, el documento IDO), el lanzamiento queda
`blocked` («Falta el documento DEC…») y se reintenta cuando se marca el de la
etapa. El detalle de la operación trae `guided_sections`: los títulos de las
secciones que escribe el ejecutivo (los pasos del guiado).

`POST /api/projects/historical-report/` acepta `stage` (una etapa con documento
principal; por defecto la primera). Cuando la lectura termina, sus
determinaciones, fundamentos, consideraciones y componentes verdes pasan a la
operación y el informe queda emitido: el Resumen y el tablero lo muestran igual
que una operación trabajada en Ecofilia.

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

## IDO → DEC (20 de noviembre de 2026)

Cómo pasa una operación de IDO a DEC. La estructura del workflow DEC está en `docs/workflow-dec.md`.

### Configuración

`stages[].hereda`: `{"de": "ido_cci", "prefijo": "B.", "etiqueta": "iet-datbc-ido"}`. Va en la etapa DEC de CAF (`user/0017`).

- **Contexto.** Si la operación tiene un informe de Ecofilia terminado en `de`, la corrida del informe de esta etapa arranca con los pasos cuyo título empieza con `prefijo`, copiados de ese informe. Se copia lo publicado por el ejecutivo si editó la sección; si no, lo que escribió el agente. Esos pasos no se vuelven a generar. Cada paso copiado lleva `heredado_de: {execution_id, stage}`.
- **Informe emitido.** Al lanzar el informe, el informe emitido de `de` se vincula a la operación con `etiqueta`. Si se armó en Ecofilia, como documento de texto con `source = "informe-etapa:<id>"`; si se subió, ese mismo documento. Mientras el documento se procesa, el lanzamiento queda `blocked` y se retoma solo.

### La mirada del ejecutivo por etapa

Al avanzar desde una etapa con informe, `context_notes.por_etapa[<etapa>]` guarda:

- `assessment`, con la misma forma que `emitted_assessment`;
- `notas`, las claves del ejecutivo tal como estaban;
- `informe_id`, `emitido_en` y `congelado_en`.

Además:

- `context_notes.precarga_de` dice de qué etapa viene la copia con la que arranca la etapa nueva.
- `registro_trazabilidad` no se copia: cada informe tiene el suyo.
- `estado`, `por_etapa` y `precarga_de` los escribe sólo el servidor. Editar la operación los preserva.

### Fotos del historial

`GET /projects/alignment-history/` y el historial de la operación traen:

- `source`: `edit`, `backfill`, `transition` o `emission`.
- `report_id`.

La foto `transition` lleva la determinación de la etapa que se dejó: no es resultado de la etapa nueva. La foto `emission` es la mirada oficial de la etapa, la de su informe emitido.

### `GET /api/projects/stage-summary/?organization=<slug>`

Para el tablero. Sin `organization` responde 400, y con otra institución que no sea la propia, 403 (salvo superadmin).

```json
[{
  "project_slug": "op", "estado": "dec", "ultima_actividad": "2026-11-20T…",
  "etapas": [{
    "stage": "ido_cci", "label": "IDO-CCI",
    "informe": {"estado": "emitido", "report_id": 7, "origen": "guiado", "version": 1, "emitted_at": "…"},
    "mirada": {"fuente": "emitido", "determinaciones": {"ct_a1": "Riesgo medio", "objetivo_1": "…"}, "enverdecimiento": {"componentes": [], "oportunidades": []}},
    "ia": {"estado": "completed", "fecha": "…"}
  }]
}]
```

- `informe.estado`: `sin_informe`, `borrador` o `emitido`.
- `mirada.fuente` dice de dónde sale la mirada:
  - `emitido`: lo emitido;
  - `congelado`: `por_etapa`;
  - `vigente`: lo que se viene cargando en la etapa actual.

  Si no hay nada, `null`.
- `ia` va sólo para superadmins.

### Conclusión de Ecofilia por criterio

Cada CT sale de la línea `**Conclusión:**` de su sección del informe visible, y sólo si dice una opción válida del CT. Si no, sale de `S.1` como respaldo. Es lo que usan `evaluation-records`, el chat y el registro.

---

## Configuración completa desde la consola (25 de noviembre de 2026)

**Objetivo:** que todo lo que hace distinta a una institución se configure
desde la consola, sin tocar código. Lo que seguía escrito para CAF pasa a la
configuración: el estilo del portal, la plantilla del informe, las secciones
del portal, el bloqueo de DEC y el formulario de la operación (la carátula).

**Versión:** sigue siendo `1`. Todo lo nuevo tiene valor por defecto en la
base, y el GET lo devuelve siempre completo (`portal`, `operation_form` y
`report_template` están siempre presentes). Así el front sabe que el back los
acepta. Una configuración guardada sin estas claves sigue siendo válida.

**Migración:** `user/0018_configuracion_completa_caf` le siembra a CAF lo que
tenía el código: su formulario, las secciones de su portal (con la Proyección),
sus cuatro temas fijados y DEC en preparación. Para CAF no cambia nada visible.
Reversible.

### `branding`: tres claves opcionales

| Clave | Regla | Sin valor |
|---|---|---|
| `portal_title` | se guarda recortado | el front usa `display_name` («Asistente Virtual DATBC» con el marco `caf-datbc`) |
| `primary_color` | hexadecimal de 6 dígitos sin `#`; se guarda en mayúsculas | el color de la plataforma |
| `portal_logo_url` | ruta del front (`/…`) o URL `https://` | la hoja de Ecofilia |

Las vacías no se guardan ni se devuelven.

### `report_template`

`null` (sin plantilla propia: el front usa la del asistente de informe) o la
plantilla del informe. La forma y las reglas son las del pedido del front
(rama `contrato-plantilla-informe`, y `ecofiliaV2/docs/contrato-plantilla-informe.md`);
la validación está en `apps/user/report_template.py`, con los mensajes del
front. El back guarda la plantilla, no la interpreta. Una clave desconocida en
la plantilla, en una sección, en el membrete, la portada o el estilo es error.

### `portal`

```json
{"sections": ["biblioteca", "operaciones", "informes", "tablero", "proyeccion"],
 "pinned_topics": [{"acronym": "ndcs", "label": "NDCS: CONTRIBUCIONES DETERMINADAS A NIVEL NACIONAL"}]}
```

| Campo | Reglas |
|---|---|
| `sections` | subconjunto de `biblioteca`, `operaciones`, `informes`, `tablero`, `proyeccion`; se guarda en ese orden. Base: las cuatro primeras |
| `pinned_topics[]` | `acronym` (lo que va antes de los dos puntos en el tema, se guarda en minúsculas, único) y `label`, no vacíos. Base: `[]` |

El tablero se muestra además sólo con criterios de evaluación, y la Proyección
sólo al superadmin. Una sección apagada desaparece de la navegación del portal
y su dirección muestra un aviso (front).

### `stages[].en_preparacion`

`true`: la etapa sólo la ve Ecofilia. Quien no es superadmin no la ve en su
configuración, no ve las operaciones que están en ella y no avanza desde la
anterior (el front lo resuelve, como el bloqueo de DEC; la API sigue
aceptando la etapa). Se guarda sólo si es `true`. La primera etapa no puede
estar en preparación.

`GET /api/organizations/{slug}/config/` la devuelve igual a miembros y
superadmin: el que oculta es el front.

### `operation_form`

Los datos que se cargan de cada operación. Cada campo guarda su valor en
`context_notes[key]`.

```json
{
  "blocks": [
    {"title": "Identidad", "description": "", "link": null, "stages": [], "only_edit": false,
     "collapsible": false, "report_title": "",
     "fields": [
       {"key": "pais", "label": "País", "type": "select", "required": true, "placeholder": "Elegí un país",
        "help": "", "options": ["Argentina", "…"], "other_option": null, "none_option": null, "unit": "",
        "report_label": "", "wide": false, "in_report": true, "for_analysis": true, "ai": null, "default": null},
       {"key": "estado", "label": "Etapa", "type": "etapa", "required": true, "…": "…"}
     ]}
  ],
  "caratula": [{"key": "numero_operacion", "label": "Número de operación"}, {"key": "estado", "label": "Estado de avance"}, {"key": "pais"}]
}
```

**Bloques**

| Campo | Reglas |
|---|---|
| `blocks` | al menos uno. El primero es la identidad: lleva el nombre de la operación, es el único con campos obligatorios y se muestra siempre (sin `stages` ni `only_edit`) |
| `title` | no vacío |
| `link` | `null` o `{label, href}` con `href` `https://` o `/…`. En un bloque de un solo campo, acompaña a su fila en la carátula |
| `stages` | etapas de la configuración: el bloque se pide sólo en ellas (o si ya tiene datos). Sus textos largos van numerados en la Sección A (en CAF, Misión de Evaluación y Antecedentes en DEC) |
| `only_edit` | no se muestra al crear (el alta tiene su propio paso) |
| `collapsible` | plegable, con cuántos datos tiene completos |
| `report_title` | título de sus textos largos en la Sección A |
| `fields` | al menos uno |

**Campos**

| Campo | Reglas |
|---|---|
| `key` | `^[a-z][a-z0-9_]{0,63}$`, única en el formulario. No puede ser una clave que escribe el sistema (`alineacion_*`, `por_etapa`, `enverdecimiento`, `year`, `extra`, `tipo_operacion_sugerido`, …) |
| `type` | `text`, `textarea`, `number`, `date`, `url`, `select`, `multiselect` o `etapa`. La etapa es el campo `estado`, de tipo `etapa`, y ningún otro; va en la identidad |
| `label` | no vacío. Es también el rótulo que reciben los asistentes |
| `required` | sólo en el primer bloque |
| `options` | `select` y `multiselect`: al menos una, sin repetir |
| `other_option` | `select` / `multiselect`: la opción con texto libre. En una selección guarda el texto como valor; en una múltiple, el texto va en `{key}_otros` (que no puede ser otro campo) |
| `none_option` | `multiselect`: la opción que excluye a las demás |
| `unit` | `number`: con qué se muestra («M USD») |
| `report_label` | rótulo en la carátula y los resúmenes si es distinto («Monto CAF») |
| `in_report` | default `true`: fila de la carátula, o texto de la Sección A si es `textarea` |
| `for_analysis` | default `true`: lo reciben los asistentes y el chat. `false` para datos de tramitación |
| `ai` | `fill` (sólo `textarea` y sólo `objetivo` o `componentes`: la IA lo escribe desde el documento principal) o `suggest` (sólo `select` y sólo `tipo_operacion`: la IA lo sugiere de sus opciones). `null` si no |
| `default` | `today` (sólo `date`: la fecha de alta) o `user_name` (`select` o `text`: quien la crea). `null` si no |

**Carátula** (`caratula`): el orden de las filas después del nombre. Cada
entrada es `{key}` de un campo (con `label` para cambiarle el rótulo) o `{key,
label}` de una clave sin campo (datos viejos: sale sólo si tiene valor). Los
campos de la carátula que no nombra van al final, en el orden del formulario.

**Base**: un formulario genérico, sin nada de CAF (país, etapa, tipo, monto,
fecha, ubicación, cliente, ejecutor, objetivo y componentes).

### Lo que usa el back del formulario

| Dónde | Qué cambia |
|---|---|
| Contexto de la operación en cada paso de un workflow (`apps.skill.operation_context`) | el rótulo de cada dato es el `label` de su campo; no van los de `for_analysis: false`; la etapa va con el nombre de la configuración. Lo que no es un campo de la institución sigue la regla anterior |
| Chat de la operación (`apps.project.services.chat_context`) | ídem, con la etapa por su nombre |
| Completar con IA (`apps.project.services.ai_fill`) | sólo completa los campos que el formulario de su institución tiene con `ai`; el tipo de operación se elige entre las opciones de **su** campo. Una institución sin tipo cerrado no recibe los de CAF |

### Copias de skills para una institución que ya existe

```
POST /api/admin/organizations/{slug}/skills/
{"clone": ["ecofilia-project-card"]}
```

Lo mismo que `clone_skills` del alta: sólo skills `public` o `platform`; cada
copia es de la institución (`{slug}-{skill}`), queda habilitada y, si corre
sobre operaciones, en las de cada operación nueva. Todo o nada.

| Caso | Respuesta |
|---|---|
| válido | **201**: el detalle de la institución (fase 2) más `cloned_skills: [{from, slug}]` |
| lista vacía, inexistente o de una institución o usuario | **400** `{"clone": [...]}`; no copia ninguna |
| no superadmin | **403** |
| institución inexistente | **404** |

Auditoría: `admin.organization.skills_cloned`.

### Etiquetas que usa una configuración

`DELETE /api/document/evidence-tags/{slug}/` responde **400** si la etiqueta
la nombra una configuración (documento principal de una etapa, un grupo o lo
que hereda una etapa): la de su institución o, si es global, la de cualquier
institución o de la plataforma. El mensaje dice dónde se usa. Primero se saca
de ahí.

### Slugs reservados

Se suman las rutas de primer nivel del front: `categories`, `chat`,
`document`, `documents`, `inventories`, `profile`, `repositories`,
`repository`, `settings`, `skills`, `tableros` y `nueva`.

### `enabled_features`

Ya no se usa: las secciones del portal son `portal.sections`. El campo sigue
en el modelo y en la API de la fase 2 por compatibilidad; la consola no lo
muestra y el alta lo manda vacío.
