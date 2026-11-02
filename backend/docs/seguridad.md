# Controles de seguridad — roles, permisos y protección de la información

Revisión del backend antes de la salida a producción del plan de instituciones
(fases 0 a 5). Se centra en tres cosas:
- quién puede hacer qué (roles y permisos);
- que la información de una institución no la vea otra;
- que quede registro de las acciones con poder.

> **Alcance y límites.** Es una autoevaluación técnica del código, mapeada a
> OWASP ASVS 4.0.3 (nivel 2), ISO/IEC 27001:2022 Anexo A y SOC 2 (Trust Services
> Criteria). **No es una certificación.** Varios controles de esas normas son de
> organización o de infraestructura y no se resuelven en el código; están en
> [Lo que depende de la operación](#lo-que-depende-de-la-operación). Para una
> institución internacional conviene sumar una prueba de penetración externa.

Fecha de la revisión: noviembre de 2026. Rama: `seguridad-backend`, apilada
sobre `fase-5-backend`.

---

## Cómo se hizo

1. **Inventario de rutas.** Las 146 rutas de la API, con su vista y sus
   permisos. Todas exigen autenticación salvo login, registro, verificación,
   restablecer contraseña, refresco del token y health check.
2. **Revisión de acceso directo por id o slug (IDOR).** Cada
   `objects.get(...)`, `get_object_or_404(...)` y filtro por slug de las vistas
   y serializers, con su contexto.
3. **Revisión de «público» y de compartir.** Cada concepto de visibilidad
   global (biblioteca, repositorios, evaluaciones, skills) y cada endpoint de
   compartir.
4. **Autenticación.** Métodos aceptados, MFA, bloqueo, tokens y admin de Django.
5. **Escaneos automáticos.** `bandit` (análisis estático) y `pip-audit`
   (dependencias con vulnerabilidades conocidas), y `manage.py check --deploy`
   con los settings de producción.
6. **Tests de regresión** para cada hallazgo, más un barrido automático de
   aislamiento sobre todas las rutas. Hubo pruebas de mutación para confirmar
   que los tests detectan la falla (ver [Evidencia](#evidencia)).

---

## Hallazgos y estado

| # | Severidad | Hallazgo | Estado |
|---|---|---|---|
| H1 | **Crítica** (disponibilidad) | Las migraciones de la fase 0 tenían los mismos números que dos migraciones que ya están en producción (`document/0023`, `skill/0048`). El `migrate` del deploy iba a fallar. | **Corregido**: renumeradas detrás de las de producción |
| H2 | **Alta** (confidencialidad) | El dueño de una evaluación podía marcarla `public`, y la veía cualquier usuario de cualquier institución, con sus documentos y resultados. | **Corregido** en dos capas, ver [Información entre instituciones](#información-entre-instituciones) |
| H3 | **Alta** (autenticación) | La API aceptaba Basic auth y sesión, que **saltean el MFA, la verificación de email y el bloqueo**. | **Corregido**: la API sólo acepta JWT y el login de la API navegable no existe en producción |
| H4 | **Alta** (acceso privilegiado) | El admin de Django entraba con email y contraseña, sin MFA ni bloqueo, y lo dejaba usar a un miembro de una institución con `is_staff`, que así veía todas las instituciones. | **Corregido**: MFA obligatorio, bloqueo, y los miembros de instituciones no entran |
| H5 | **Alta** (acceso privilegiado) | El admin de usuarios mostraba y dejaba editar `mfa_secret`: quien tenía acceso al admin podía generar los códigos MFA de cualquiera. | **Corregido**: no se muestra |
| H6 | **Alta** (autenticación) | `POST /api/auth/mfa/setup/` regeneraba el secreto y **apagaba el MFA sin pedir el código**: con un token robado se le sacaba el MFA a una cuenta. | **Corregido**: con MFA activo responde 400; primero hay que desactivarlo con un código |
| H7 | **Alta** (trazabilidad) | No había registro de auditoría: ni de quién cambiaba roles, permisos o configuraciones, ni de qué leía el superadmin. | **Corregido**: `AuditEvent`, append-only, con un evento por acción privilegiada y el rastro de cada pedido del superadmin o del staff |
| H8 | **Alta** (acceso privilegiado) | Borrar una institución convertía a sus administradores en superadmins de toda la plataforma (`SET_NULL` deja `role=admin` sin institución) y dejaba sus operaciones «sin institución». Lo reportó el front. | **Corregido**: no se puede borrar una institución con miembros u operaciones (`PROTECT`), y quien pierde su institución siendo admin baja a `member` |
| H9 | **Alta** (confidencialidad) | Quien salía de una institución seguía viendo las operaciones que creó ahí, con sus corridas, sus documentos, sus chats y sus evaluaciones. Lo reportó el front. | **Corregido**: lo de una institución sólo lo ve quien es de esa institución, aunque lo haya creado |
| M1 | **Alta** (acceso privilegiado) | El MFA no era obligatorio para el superadmin. | **Corregido**: sin MFA activo, un superadmin o un staff es, para la API y para el admin de Django, un usuario común. `/api/auth/me/` lo marca con `mfa_required` |
| M2 | Media (autenticación) | No había bloqueo por cuenta tras intentos fallidos; sólo throttle por IP. | **Corregido**: bloqueo tras 10 fallos, 15 minutos |
| M3 | Media (autenticación) | Un código MFA servía varias veces dentro de su ventana. | **Corregido**: cada código sirve una sola vez |
| M4 | Media (sesión) | Cambiar o restablecer la contraseña no invalidaba los access tokens ya emitidos. | **Corregido**: `CHECK_REVOKE_TOKEN` |
| M5 | Media (configuración) | En producción, las cookies seguras, el HSTS y la redirección a HTTPS dependían de variables de entorno que venían apagadas por defecto. | **Corregido**: fijas en `prod.py`; `check --deploy` pasa |
| M6 | Media (permisos) | Al habilitar skills en operaciones y repositorios quedaba la regla anterior a la fase 0 (`owner` nulo = visible), que permitía habilitar skills de plataforma o de otra institución. | **Corregido**: usa `apps.skill.visibility` |
| M7 | Media (dependencias) | PyPDF2 3.0.1 (PYSEC-2026-1835): un PDF armado se cuelga en un bucle infinito y traba un worker de ingesta. PyPDF2 está abandonado. | **Corregido**: reemplazado por `pypdf` |
| M8 | Media (permisos) | La consola de la fase 2 podía crear superadmins sin querer: `role=admin` sin institución es superadmin de toda la plataforma. | **Corregido** en la fase 2: 400 |
| B1 | Baja (enumeración) | Crear evaluaciones o documentos sobre una operación ajena respondía «No tienes acceso», distinto de «no encontrado»: confirmaba que el slug existía. | **Corregido**: mismo mensaje |
| B2 | Baja | El email de restablecer contraseña decía «1 hora» y el enlace valía 3 días (default de Django). | **Corregido**: 24 horas, configurable, y el email lo dice |
| P1 | Media | `mfa_secret` se guarda en claro en la base. | **Pendiente**, ver [Riesgos que quedan](#riesgos-que-quedan) |
| P2 | Media | Dos nociones de acceso global: `is_staff` y superadmin (`is_ecofilia_admin`). | **Pendiente**, mitigado |
| P3 | Media | No hay archivo de lock de dependencias (`poetry.lock`). | **Pendiente** |
| P4 | Baja | Sin Content-Security-Policy. | Aceptado: la API sólo responde JSON |

---

## Controles

### Roles

| Nivel | Cómo se reconoce | Qué puede |
|---|---|---|
| Superadmin de Ecofilia | `is_superuser` o `role=admin`, **sin** institución (`apps.user.access.is_ecofilia_admin`) | Todo lo de la plataforma: consola, configuraciones, alcance de skills, biblioteca pública, alta de instituciones. **Todo exige MFA activo**: sin MFA es un usuario común. |
| Administrador de institución | `role=admin` con institución | Todas las operaciones y documentos de su institución; nada fuera de ella |
| Miembro de institución | con institución | Lo propio y lo compartido con él, siempre dentro de su institución |
| Público | sin institución | Lo propio, lo compartido y lo público de la plataforma |

- **Separación de funciones.** La consola no crea superadmins (M8). Hacer
  superadmin a alguien se decide en el admin de Django, que exige MFA (H4) y
  deja su propio registro de cambios (`LogEntry`).
- **Una institución no se borra con gente adentro.** `User.organization` y
  `Project.organization` son `PROTECT`. Dar de baja una institución es mover o
  desactivar a cada miembro y decidir qué pasa con cada operación. Además,
  `User.save` baja a `member` a quien pierde su institución siendo `admin`,
  venga el cambio de donde venga.
- **Mínimo privilegio en la fuente.** El alcance de institución se aplica al
  autenticar (`apps.authentication.org_scope`): un miembro de una institución
  nunca llega a una vista con `is_staff` o `is_superuser`, aunque los tenga
  cargados por error.

### Autenticación

| Control | Implementación | Test |
|---|---|---|
| Un solo método en la API: JWT | `REST_FRAMEWORK` en `settings/base.py`; `api-auth/` sólo con `DEBUG` | `OnlyJWTTestCase` |
| **MFA obligatorio para el superadmin** | Sin MFA, `require_mfa_for_privileges` le saca en memoria el alcance de plataforma en cada pedido. No se persiste; sí se avisa con `mfa_required` | `MFAIsMandatoryForSuperadminsTestCase` |
| Email verificado para entrar | Login JWT (`TokenObtainPairWithMFASerializer`) | `test_onboarding` |
| MFA (TOTP) | Opcional para miembros y público; obligatorio para superadmin, staff y el admin de Django | `PrivilegedNeedsMFATestCase`, `DjangoAdminTestCase` |
| Código MFA de un solo uso | `MFAService.verify` guarda el paso aceptado (`User.mfa_last_step`) | `MFATestCase` |
| El MFA no se apaga sin código | `MFASetupView` responde 400 con MFA activo | `MFATestCase` |
| Bloqueo por cuenta | 10 fallos (contraseña o MFA) → 15 minutos (`LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_MINUTES`). El error es el mismo que una contraseña equivocada: no confirma que la cuenta existe | `LockoutTestCase` |
| Throttle por IP | DRF: 30/min anónimo, 120/min autenticado, 20/min refresco | (existente) |
| Contraseñas | Validadores de Django: largo, comunes, numéricas, similares al usuario | (existente) |
| Tokens | Access de 15 min y refresh de 7 días con rotación y blacklist. Cambiar la contraseña o desactivar la cuenta los invalida | `TokenRevocationTestCase` |
| Enlaces de un solo uso | Restablecer contraseña e invitación: `PasswordResetTokenGenerator`, 24 h (`PASSWORD_RESET_TIMEOUT`) | `test_onboarding` |

### Autorización

- **Por defecto, cerrado.** `DEFAULT_PERMISSION_CLASSES = IsAuthenticated`. Cada
  recurso filtra su queryset por usuario (`for_user`, `visible_documents_q`,
  `visible_skills`, `executions_queryset_for_user`) y verifica permisos por
  objeto (`can_view`, `can_edit`, `can_write`).
- **Lo que no se ve da 404.** Así no se distingue «no existe» de «es de otro».
- **Acceso por slug o id revisado** (ver [Cómo se hizo](#cómo-se-hizo)). Todo
  slug que llega del cliente se resuelve dentro de lo que el usuario ve (chat,
  operaciones, evaluaciones, repositorios), o se verifica el permiso antes de
  usarlo.

### Información entre instituciones

| Control | Implementación | Test |
|---|---|---|
| Skills por alcance | `apps/skill/visibility.py` (fase 0) | `test_aislamiento` |
| Etiquetas por institución | `apps.document.services.evidence_tags_for_organization` | `test_aislamiento`, `test_alcance` |
| Compartir no cruza instituciones y no revela emails | `apps/user/sharing.py`, en los cuatro recursos | (existente) |
| **«Público» no sale de su institución** | Publicar una evaluación es del superadmin, con MFA. Al leer, algo «público» de un miembro sólo llega a su institución (`public_reach_q`), también en repositorios y en los datos viejos | `PublicStaysInItsInstitutionTestCase` |
| Biblioteca pública | Sólo superadmin, con MFA; cada publicación queda en la auditoría con la institución de cada documento | `PrivilegedNeedsMFATestCase` |
| Cada institución aprende sólo de sus datos | `evaluation-records` por institución; versiones de skills informadas por su institución | `test_aislamiento`, `test_alcance` |
| Corridas acotadas | Una corrida lee sólo los documentos de su operación, repositorio o documento | (revisión de código) |
| **Salir de una institución corta el acceso** | Operaciones, corridas, documentos de sus operaciones, chats y evaluaciones aplican `institution_boundary_q`: ser el dueño no alcanza si ya no es de esa institución | `LeavingAnInstitutionTestCase` |
| **Barrido automático** | Todas las rutas GET de la API sin parámetros, recorridas por un miembro de A y por un público, con datos de B sembrados en cada recurso | `CrossTenantSweepTestCase` |

### Registro de auditoría

- `apps.user.models.AuditEvent` se escribe con `apps.user.audit.record`.
- Cada evento guarda quién (usuario y email), qué (acción), sobre qué (objeto
  e institución), cuándo, desde dónde (IP y user-agent) y el antes y después.
- **Append-only.** El modelo no deja editar ni borrar, y en el admin es de sólo
  lectura.
- **Atómico.** Las acciones privilegiadas lo escriben en su misma transacción:
  si la acción falla, no queda evento, y viceversa.
- **Qué se registra:**
  - logins exitosos y fallidos, bloqueos, restablecer y cambiar contraseña,
    activar y desactivar MFA;
  - alta, cambios y miembros de instituciones, y cambios de usuarios;
  - alcance y asignación de skills, y versiones informadas por una institución;
  - configuraciones de institución y de plataforma;
  - etiquetas creadas y movidas;
  - documentos publicados y evaluaciones publicadas.
- **Qué lee el superadmin:** cada pedido a la API de un superadmin o del staff,
  lecturas incluidas, deja un evento `superadmin.access` con método, ruta,
  institución y código de respuesta
  (`main.middleware.PrivilegedAccessAuditMiddleware`, `AUDIT_PRIVILEGED_ACCESS`).
- **Consulta:** `GET /api/admin/audit/?action=&organization=&actor=&since=`
  (superadmin con MFA). Los cambios hechos desde el admin de Django quedan
  además en su `LogEntry`.

### Configuración de producción

- **Fijo en `prod.py`, sin depender de variables:**
  - `DEBUG = False`;
  - cookies `Secure` y `HttpOnly`, CSRF por sesión;
  - HSTS de un año con subdominios;
  - redirección a HTTPS detrás del ALB (`X-Forwarded-Proto`);
  - `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: same-origin`.
- `manage.py check --deploy` con los settings de producción pasa. El único aviso
  es el preload de HSTS, silenciado a propósito: se pide para el dominio raíz,
  no para la API.
- Archivos en S3 con URLs firmadas (`AWS_QUERYSTRING_AUTH = True`), sin ACL
  pública.

### Dependencias y código

- `bandit` sobre `apps` y `main` no encuentra nada real: el único aviso es un
  `RawSQL` armado con constantes, sin datos del usuario.
- `pip-audit`: PyPDF2 reemplazado por `pypdf` (M7). La otra alerta,
  `setuptools`, viene de la imagen base y no de la aplicación; se resuelve
  actualizando la imagen.

---

## Riesgos que quedan

| # | Riesgo | Por qué no se resolvió acá | Mitigación y siguiente paso |
|---|---|---|---|
| P1 | `mfa_secret` en claro en la base | Cifrarlo pide gestión de claves (KMS o una variable en Secrets Manager) y migrar los secretos existentes; un error deja sin MFA a todos | La base cifrada en reposo (RDS) mitiga lectura de disco, y el admin ya no muestra el secreto. Siguiente paso: cifrado a nivel campo con una clave de Secrets Manager |
| P2 | `is_staff` sin superadmin también ve todo | Unificarlo cambia decenas de chequeos existentes | `listar_usuarios_por_nivel` marca a esos usuarios. Política: `is_staff` sólo para el equipo de Ecofilia, revisado en cada revisión de accesos |
| P3 | Sin lock de dependencias | Cada build instala la última versión compatible: no es reproducible y una versión nueva comprometida entraría sola | Generar y versionar `poetry.lock`, y correr `pip-audit` en CI |
| P4 | Sin CSP | La API no sirve HTML (sólo el admin) | El front debe definir su propia CSP |
| — | Retención y borrado de datos personales (GDPR, Ley 25.326) | Es una decisión de política | Definir la retención de `AuditEvent` y de datos de usuarios, y el procedimiento de borrado a pedido |

---

## Lo que depende de la operación

Estos controles de ISO 27001 y SOC 2 no se resuelven en el código. Hay que
verificarlos en AWS y en los procesos:

- [ ] **RDS:** cifrado en reposo, backups automáticos con retención,
      `POSTGRES_SSLMODE=require`.
- [ ] **S3:** el bucket de documentos con *Block Public Access* activado y
      cifrado.
- [ ] **Secrets Manager:** `SECRET_KEY` y `JWT_SIGNING_KEY` distintas,
      largas y con rotación definida.
- [ ] **IAM:** `ecofiliaTaskRole` con mínimo privilegio (S3 del bucket, SQS,
      SES, Textract; nada más).
- [ ] **CloudWatch:** retención de logs definida y alertas sobre
      `auth.account.locked` y errores 5xx.
- [ ] **ALB:** sólo HTTPS (con el puerto 80 redirigiendo), y considerar AWS WAF
      con reglas administradas.
- [ ] **Revisión de accesos trimestral:** `listar_usuarios_por_nivel`, y
      superadmins con MFA (`mfa_enabled`).
- [ ] **Respuesta a incidentes:** quién decide y cómo se revocan accesos
      (desactivar el usuario invalida sus tokens).
- [ ] **Prueba de penetración externa** antes de abrir a instituciones nuevas.
- [ ] **Acuerdo de tratamiento de datos** con cada institución, y
      subencargados declarados (AWS, Anthropic, OpenAI).

---

## Antes del deploy

1. **Todos los superadmins y el staff activan MFA**
   (`POST /api/auth/mfa/setup/` → `POST /api/auth/mfa/verify/`). Sin MFA son
   usuarios comunes: no ven otras instituciones ni usan la consola o el admin
   de Django. Sólo en una emergencia: `ADMIN_REQUIRE_MFA=0`.
2. **Al desplegar, todos los usuarios vuelven a iniciar sesión una vez.**
   `CHECK_REVOKE_TOKEN` invalida los tokens emitidos sin el sello nuevo.
3. Entran las migraciones `user/0011_seguridad_login`, `user/0012_audit_event`,
   `user/0013_institucion_protegida` y `project/0019_institucion_protegida`.
4. El volumen de `superadmin.access` crece con el uso de la consola: definir su
   retención (ver [Riesgos que quedan](#riesgos-que-quedan)).

---

## Evidencia

- `apps/user/tests/test_seguridad.py`: 41 tests, uno o más por control,
  incluidos los cuatro puntos que reportó el front (borrar una institución,
  Basic auth, salir de una institución, auditoría de cambios y lecturas, MFA
  obligatorio).
- **Pruebas de mutación:**
  - quitar la regla de «público dentro de su institución» hace fallar el
    barrido y el test de evaluaciones;
  - quitar el MFA de la consola hace fallar el test de privilegios;
  - en la fase 0, romper la visibilidad de skills hacía fallar 9 de 26 tests
    de aislamiento.
- **Suite completa** (`apps.project apps.skill apps.document apps.user
  apps.authentication apps.evaluation apps.repository apps.chat`): las mismas
  32 fallas que tiene hoy `main`, ninguna nueva.
