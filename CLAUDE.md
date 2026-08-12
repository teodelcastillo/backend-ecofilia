# Ecofilia RAGv2 — Contexto para Claude Code

## Qué es este proyecto

Backend Django + Celery de la plataforma Ecofilia. RAG (Retrieval-Augmented Generation) para análisis de documentos ESG con embeddings pgvector + OpenAI.

Stack: Django · DRF · SimpleJWT · Celery · PostgreSQL + pgvector · Docker · AWS ECS Fargate

---

## Infraestructura AWS (producción)

**Cuenta:** `028780196116`
**Región:** `us-east-2` (Ohio)
**Dominio API:** `api.ecofilia.site`

### Servicios ECS (cluster: `cluster-ecofilia`)

| Servicio | Task Definition | Función |
|---|---|---|
| `ecofilia-api` | `ecofilia-api` | Django/Gunicorn — API REST |
| `ecofilia-worker` | `ecofilia-worker` | Celery worker (procesa docs, evaluaciones) |
| `ecofilia-beat` | `ecofilia-beat` | Celery beat scheduler |

- Imagen Docker: `028780196116.dkr.ecr.us-east-2.amazonaws.com/ecofilia-api:<git-sha>`
- Subnets ECS (privadas, sin IP pública): `subnet-0eeb7c030c003896d`, `subnet-084c6ea560cf05512`, `subnet-0bf0864eb1124711d`
- Security Group API: `sg-0e751d4a7ed56c286`
- Security Group Worker: `sg-060d35df0ec63c774`

### Base de datos

- **RDS PostgreSQL 17.4** — `ecofilia-db.cjsem8ewibzq.us-east-2.rds.amazonaws.com`
- Instancia: `db.t4g.micro` | DB: `postgres` | Sin acceso público
- Extensión: `pgvector`

### Otros recursos

| Recurso | Identificador |
|---|---|
| ECR repo | `ecofilia-api` |
| SQS queue | `ecofilia-celery-sqs` |
| Secrets Manager | `ecofilia/prod` |
| ALB | `ecofilia-alb` |
| Log groups | `/ecs/ecofilia-api`, `/ecs/ecofilia-worker`, `/ecs/ecofilia-beat` |

---

## Comandos AWS frecuentes

### Ejecutar migraciones de Django

```bash
aws ecs run-task \
  --cluster cluster-ecofilia \
  --task-definition ecofilia-api \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0eeb7c030c003896d],securityGroups=[sg-0e751d4a7ed56c286],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","manage.py","migrate"]}]}' \
  --region us-east-2
```

### Ver logs en tiempo real

```bash
# API
aws logs tail /ecs/ecofilia-api --follow --region us-east-2

# Worker
aws logs tail /ecs/ecofilia-worker --follow --region us-east-2

# Solo errores
aws logs filter-log-events --log-group-name /ecs/ecofilia-api --filter-pattern "ERROR" --region us-east-2
```

### Estado de servicios ECS

```bash
# Listar tareas corriendo
aws ecs list-tasks --cluster cluster-ecofilia --region us-east-2

# Estado de un servicio
aws ecs describe-services --cluster cluster-ecofilia --services ecofilia-api --region us-east-2

# Forzar nuevo deploy (redeploy sin cambio de imagen)
aws ecs update-service --cluster cluster-ecofilia --service ecofilia-api --force-new-deployment --region us-east-2
```

### Ejecutar cualquier comando de Django en ECS

```bash
aws ecs run-task \
  --cluster cluster-ecofilia \
  --task-definition ecofilia-api \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0eeb7c030c003896d],securityGroups=[sg-0e751d4a7ed56c286],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","manage.py","COMANDO_AQUI"]}]}' \
  --region us-east-2
```

### Secretos

```bash
# Ver secretos de producción
aws secretsmanager get-secret-value --secret-id ecofilia/prod --region us-east-2
```

---

## CI/CD

Push a `main` con cambios en `backend/**` → GitHub Actions → build Docker → push ECR → deploy ECS (los 3 servicios).

Workflow: `.github/workflows/deploy.yml`
Rol IAM: `arn:aws:iam::028780196116:role/ecofilia-github-deploy` (OIDC, sin credenciales estáticas)

---

## Estructura del backend

```
backend/
├── main/           # Settings, URLs, Celery config, WSGI
│   └── settings/
│       ├── base.py
│       └── prod.py
├── apps/
│   ├── authentication/  # JWT, login, register, MFA
│   ├── user/
│   ├── document/        # Upload, parsing, embeddings
│   ├── chat/            # RAG queries
│   ├── evaluation/      # Evaluaciones ESG
│   └── project/
├── docker/
│   ├── Dockerfile.prod
│   └── template.env
└── manage.py
```

## Email — Amazon SES

Backend configurado con `django-ses`. Templates HTML en `apps/authentication/templates/emails/`.

**Emails implementados:**
- `verification.html` — confirmación de cuenta al registrarse
- `password_reset.html` — restablecimiento de contraseña

**Variables de entorno necesarias en ECS (Secrets Manager o task definition):**
```
EMAIL_BACKEND=django_ses.SESBackend
DEFAULT_FROM_EMAIL=no-reply@ecofilia.site
AWS_SES_REGION_NAME=us-east-2
```

**Permisos IAM requeridos en `ecofiliaTaskRole`:**
```json
{ "Effect": "Allow", "Action": ["ses:SendEmail", "ses:SendRawEmail"], "Resource": "*" }
```

**Pasos para activar SES en producción:**
1. Verificar dominio `ecofilia.site` en SES (consola → Verified identities → Create identity)
2. Agregar registros DNS en Vercel (DKIM + MAIL FROM)
3. Pedir salida del sandbox: SES → Account dashboard → Request production access
4. Agregar las 3 variables de entorno en Secrets Manager `ecofilia/prod`
5. Hacer deploy

---

## Ingesta de documentos — cobertura y OCR

El pipeline mide cuánto del documento realmente leyó y lo persiste en
`page_count` / `pages_with_text` / `parser_used`.

- Si lee menos del 80% de las páginas (`DOC_MIN_PAGE_COVERAGE`) o extrae menos
  de 400 caracteres por página (`DOC_MIN_CHARS_PER_PAGE`), el documento queda en
  estado **`partial`**, no en `done`: está indexado pero incompleto.
- Los PDFs escaneados pasan por **Amazon Textract** (`apps/document/utils/ocr.py`)
  antes de darse por perdidos. Se dispara solo cuando la extracción normal quedó
  corta, así que no se paga OCR por documentos sanos.

**Permiso IAM requerido en `ecofiliaTaskRole`:**
```json
{ "Effect": "Allow", "Action": ["textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"], "Resource": "*" }
```

**Variables de entorno:** `DOCUMENT_OCR_ENABLED` (default `1`), `OCR_MAX_PAGES`
(default `1000`), `AWS_TEXTRACT_REGION` (default: la de S3).

### Tablas

`apps/document/utils/tables.py` detecta y extrae tablas con PyMuPDF (local,
gratis) y las lineariza repitiendo los encabezados en cada fila, para que cada
fila sea consultable por sí sola. Los chunks resultantes llevan
`chunk_type="table"`.

La validación es deliberadamente estricta: **ante la duda no se inventa un
encabezado**. Si los nombres no tienen forma de etiqueta, no cubren la mayoría
de las columnas, se repiten o aparecen como dato en el cuerpo, la fila cae a
valores separados por `|` — incompleto pero nunca falso. Sin esa validación el
detector ascendía filas de datos a encabezado y cada fila terminaba afirmando
una relación inventada.

> **Limitación abierta — códigos urbanos tipo CABA (doc 726).** El Código
> Urbanístico de Buenos Aires tiene la capa de texto dañada: los diacríticos
> aparecen separados de sus letras y las palabras vienen cortadas por guiones
> (`nimo, de la cantidad de los m s: 30%, como m ² o m á í ó du-`). De sus 48
> tablas aceptadas, **ninguna** produce encabezados utilizables, y ningún
> extractor local puede recomponer eso. Quedan dos hipótesis sin dirimir: que
> el PDF de origen esté mal generado, o que haga falta otra vuelta de
> procesamiento (Textract `AnalyzeDocument` con `TABLES`, o normalización
> Unicode de diacríticos combinantes y de-hyphenation). **El caso de uso de
> códigos urbanos no está resuelto.** Conviene además verificar si esa
> corrupción afecta al texto corrido del documento y no solo a sus tablas.

**Reprocesar documentos** (resetea el estado, así también destraba los colgados
en `processing`):
```bash
python manage.py reprocess_documents --ids 55,88,726 --dry-run
python manage.py reprocess_documents --status partial,error,processing
```

> `pymupdf` es dependencia obligatoria. Si falta, el parser cae a PyPDF2: sin
> números de página y con extracción mucho peor. Estuvo sin declarar hasta
> agosto de 2026 y el fallback era silencioso.

---

## Variables de entorno clave (en Secrets Manager `ecofilia/prod`)

`SECRET_KEY`, `JWT_SIGNING_KEY`, `OPENAI_API_KEY`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_NAME`, `POSTGRES_USER`, `POSTGRES_PASSWORD`

Variables de configuración (en task definition ECS):
`DJANGO_SETTINGS_MODULE=main.settings.prod`, `JWT_ACCESS_TTL_MINUTES`, `JWT_REFRESH_TTL_DAYS`, `SQS_QUEUE_URL`, `AWS_STORAGE_BUCKET_NAME`
