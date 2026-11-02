# Salida a producción del plan de instituciones — procedimiento y vuelta atrás

Regla: **nada se hace en producción sin una vuelta atrás probada.** Este documento
dice qué se hace, en qué orden, cómo se verifica y cómo se vuelve atrás en cada
paso. Back y front salen juntos.

Cada nivel de vuelta atrás está probado en local sobre una base con el esquema de
producción (`main` 4e02713) y datos parecidos a los reales: ver
[Qué se probó](#qué-se-probó).

---

## Resumen

| Paso | Toca producción | Vuelta atrás |
|---|---|---|
| 1. Relevamiento de datos | No: sólo lectura | — |
| 2. Snapshot de RDS | Crea un snapshot; no cambia la base | Borrar el snapshot (no hace falta) |
| 3. Anotar las revisiones actuales de ECS | No | — |
| 4. Merge a `main` → deploy + migraciones | Sí | Niveles 1, 2 y 3 |
| 5. Verificación | No: sólo lectura | — |
| 6. Cambios de AWS (S3, RDS SSL, logs, IAM) | Sí, **uno por vez y después de la salida** | Cada uno trae la suya |

---

## 1. Relevamiento (sólo lectura)

Con la imagen que está hoy en producción, sin cambiar nada. Responde tres
preguntas que deciden cómo queda cada persona después del deploy:
- ¿existe la institución `caf`? Si no, las skills `caf-*` quedan en `platform` y
  los usuarios de CAF dejan de verlas;
- ¿quién tiene `role=admin` sin institución? Esas personas pasan a ser
  **superadmins**;
- ¿quién tiene `is_staff` sin ser superadmin? Esas personas **dejan de ver todo**.

```bash
CODE='
from django.db.models import Q, Count
from django.contrib.auth import get_user_model
from apps.user.models import Organization
from apps.skill.models import Skill
from apps.document.models import EvidenceTag
U = get_user_model()
print("instituciones:", list(Organization.objects.annotate(n=Count("members")).values_list("slug", "n")))
print("existe caf:", Organization.objects.filter(slug="caf").exists())
print("admin sin institucion (seran superadmin):", list(U.objects.filter(role="admin", organization__isnull=True).values_list("email", "is_superuser")))
print("superusers:", list(U.objects.filter(is_superuser=True).values_list("email", "organization__slug")))
print("staff que no es superuser:", list(U.objects.filter(is_staff=True, is_superuser=False).values_list("email", "role", "organization__slug")))
print("staff o superuser CON institucion:", list(U.objects.filter(organization__isnull=False).filter(Q(is_staff=True) | Q(is_superuser=True)).values_list("email", "organization__slug")))
print("usuarios:", U.objects.count(), "| con MFA activo:", U.objects.filter(mfa_enabled=True).count())
print("skills caf-*:", list(Skill.objects.filter(slug__startswith="caf-").values_list("slug", "owner_id")))
print("skills con dueno:", Skill.objects.filter(owner__isnull=False).count(), "| sin dueno:", Skill.objects.filter(owner__isnull=True).count())
print("etiquetas de evidencia:", EvidenceTag.objects.count())
'
OVERRIDES=$(jq -nc --arg c "$CODE" '{containerOverrides:[{name:"api",command:["python","manage.py","shell","-c",$c]}]}')
aws ecs run-task --cluster cluster-ecofilia --task-definition ecofilia-api --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0eeb7c030c003896d],securityGroups=[sg-0e751d4a7ed56c286],assignPublicIp=DISABLED}" \
  --overrides "$OVERRIDES" --region us-east-2 --query 'tasks[0].taskArn' --output text
# La salida queda en el log de la tarea:
aws logs tail /ecs/ecofilia-api --since 10m --region us-east-2
```

Si algo de esto no es lo esperado, **se corrige antes** del deploy, en el admin de
Django de producción (es reversible: el admin deja el cambio en su historial).

---

## 2. Snapshot de RDS

Primero, el estado de la instancia y sus backups automáticos:

```bash
aws rds describe-db-instances --db-instance-identifier ecofilia-db --region us-east-2 \
  --query 'DBInstances[0].{Estado:DBInstanceStatus,RetencionBackups:BackupRetentionPeriod,Cifrada:StorageEncrypted,MultiAZ:MultiAZ,RestaurableHasta:LatestRestorableTime}'
```

- `RetencionBackups` mayor que 0 significa que además hay **restauración a un
  punto en el tiempo**: una segunda red de seguridad, independiente del snapshot.
- La instancia es single-AZ: al empezar el snapshot la base puede frenar la
  escritura unos segundos. Conviene hacerlo con poco uso.

```bash
SNAP=ecofilia-antes-de-instituciones-$(date -u +%Y%m%d-%H%M)
aws rds create-db-snapshot --db-instance-identifier ecofilia-db --db-snapshot-identifier "$SNAP" --region us-east-2
aws rds wait db-snapshot-available --db-snapshot-identifier "$SNAP" --region us-east-2
aws rds describe-db-snapshots --db-snapshot-identifier "$SNAP" --region us-east-2 \
  --query 'DBSnapshots[0].{Estado:Status,Avance:PercentProgress,Creado:SnapshotCreateTime,Cifrado:Encrypted}'
echo "$SNAP"   # anotarlo: es el que se restaura en el nivel 3
```

- Un snapshot manual **no vence**: queda hasta que alguien lo borre. No borrarlo
  hasta varias semanas después de la salida.
- **No se avanza** hasta que diga `available`.

---

## 3. Anotar lo que está corriendo

```bash
for s in ecofilia-api ecofilia-worker ecofilia-worker-interactive ecofilia-beat; do
  echo "$s $(aws ecs describe-services --cluster cluster-ecofilia --services $s --region us-east-2 \
    --query 'services[0].taskDefinition' --output text)"
done | tee revisiones-antes-de-instituciones.txt
```

Son las revisiones a las que se vuelve en el nivel 1. Las imágenes viejas siguen
en ECR (etiquetadas por SHA), así que la vuelta no reconstruye nada.

---

## 4. Salida

1. Merge de `instituciones` → `main`, en back y en front, en el mismo momento.
2. El workflow de deploy:
   - construye la imagen;
   - **corre `migrate`**: 15 migraciones, todas probadas de ida y de vuelta;
   - si `migrate` falla, **no despliega nada**: los servicios siguen con la
     imagen vieja (ver nivel 2);
   - despliega API, worker, worker interactivo y beat.
3. Mirar el run de GitHub Actions y los logs:
   `aws logs tail /ecs/ecofilia-api --follow --region us-east-2`.

Cada migración corre en su propia transacción: si una falla, esa no deja nada a
medias, pero las anteriores quedan aplicadas.

---

## 5. Verificación (sólo lectura)

- Login de un superadmin → `home_path: "/admin"`, la consola abre.
- Login de alguien de CAF → `home_path: "/caf"`, ve sus operaciones y las skills
  `caf-*`.
- Login de un usuario público → `home_path: "/app"`, no ve nada de CAF.
- Con `aws ecs run-task` (como en el paso 1, con la imagen nueva):
  `listar_usuarios_por_nivel`, `listar_skills_alcance` y
  `organization_isolation_report`. Comparar con el relevamiento del paso 1.

---

## Vuelta atrás

Hay tres niveles. Se empieza por el menor que alcance.

### Nivel 1 — volver al código anterior, sin tocar datos (minutos)

Para cualquier problema de comportamiento. La base queda con el esquema nuevo y
el código viejo funciona sobre él: las columnas nuevas obligatorias tienen
default en la base (`user/0014`, `skill/0051`).

1. **Sólo si alguien tiene MFA activo:** descifrar los secretos, porque el código
   viejo los lee tal cual. Se corre con la imagen **nueva** (la que tiene la
   migración inversa):
   ```bash
   OVERRIDES='{"containerOverrides":[{"name":"api","command":["python","manage.py","migrate","user","0014","--noinput"]}]}'
   aws ecs run-task --cluster cluster-ecofilia --task-definition ecofilia-api --launch-type FARGATE \
     --network-configuration "awsvpcConfiguration={subnets=[subnet-0eeb7c030c003896d],securityGroups=[sg-0e751d4a7ed56c286],assignPublicIp=DISABLED}" \
     --overrides "$OVERRIDES" --region us-east-2
   ```
   Sólo deshace `user/0015` (el cifrado). El código nuevo sigue leyendo bien los
   secretos en claro mientras tanto.
2. Volver cada servicio a su revisión anterior (del archivo del paso 3):
   ```bash
   while read svc td; do
     aws ecs update-service --cluster cluster-ecofilia --service "$svc" --task-definition "$td" --region us-east-2 >/dev/null
   done < revisiones-antes-de-instituciones.txt
   aws ecs wait services-stable --cluster cluster-ecofilia \
     --services ecofilia-api ecofilia-worker ecofilia-worker-interactive ecofilia-beat --region us-east-2
   ```
3. Decidir qué pasa con `main`. Mientras `main` tenga el código nuevo, **cualquier
   push a `main` lo vuelve a desplegar**. Hay dos opciones: arreglar hacia
   adelante con un PR, o `git revert -m 1 <merge>` en `main`. Ojo: después, para
   volver a salir, hay que revertir ese revert.

Qué implica el nivel 1:
- Vuelve el comportamiento de hoy, **sin aislamiento entre instituciones**: es lo
  que ya corre en producción.
- Lo creado con la versión nueva (configuraciones, auditoría, etiquetas por
  institución) queda en la base y vuelve a regir en el próximo deploy.
- Lo que el código viejo cree en ese lapso: las skills nuevas quedan en
  `platform` y los usuarios nuevos sin bloqueo de intentos.
- El código viejo **no puede borrar** un usuario o una institución que ya tengan
  eventos de auditoría: la base lo rechaza entero, sin perder nada.

### Nivel 2 — revertir también las migraciones (con pérdida acotada)

Para cuando `migrate` falló a mitad del deploy, o el esquema nuevo da problemas.
Con la imagen **nueva**, en este orden (el mismo que se probó):

```bash
for target in "user 0014" "user 0013" "skill 0048" "project 0018" "user 0007" "document 0023"; do
  OVERRIDES=$(jq -nc --arg a "${target% *}" --arg b "${target#* }" \
    '{containerOverrides:[{name:"api",command:["python","manage.py","migrate",$a,$b,"--noinput"]}]}')
  # run-task con "$OVERRIDES" como arriba; esperar a que termine (exit 0) antes del siguiente
done
```

Después, el nivel 1 desde el paso 2. Lo que se pierde:
- la auditoría;
- las configuraciones de instituciones y de plataforma;
- la asignación de etiquetas a instituciones;
- el contador de intentos de login.

Las filas que ya existían vuelven exactamente a como estaban (probado). Si dos
instituciones crearon etiquetas con el mismo slug, `document 0023` falla: en ese
caso, nivel 3.

### Nivel 3 — restaurar el snapshot (último recurso)

Para datos dañados. **Se pierde todo lo escrito después del snapshot.**

1. Restaurar a una instancia **nueva**. Nunca encima de `ecofilia-db`, que queda
   intacta para comparar:
   ```bash
   aws rds restore-db-instance-from-db-snapshot --db-instance-identifier ecofilia-db-restaurada \
     --db-snapshot-identifier "$SNAP" --db-instance-class db.t4g.micro --no-publicly-accessible \
     --vpc-security-group-ids <los de ecofilia-db> --db-subnet-group-name <el de ecofilia-db> --region us-east-2
   aws rds wait db-instance-available --db-instance-identifier ecofilia-db-restaurada --region us-east-2
   ```
   (los dos `<...>` salen de `describe-db-instances` de `ecofilia-db`).
2. Volver los servicios a las revisiones anteriores (nivel 1, paso 2).
3. Apuntar `POSTGRES_HOST` en `ecofilia/prod` al endpoint de la restaurada y forzar
   un redeploy de los cuatro servicios
   (`update-service --force-new-deployment`). Para volver de esto, el valor
   anterior del secreto queda como versión previa en Secrets Manager.

---

## 6. Cambios de AWS (después de la salida, uno por vez)

Ninguno es necesario para salir. Cada uno se hace solo, se verifica y tiene su
vuelta atrás. **Primero se mira el estado actual (sólo lectura) y se anota.**

| Cambio | Ver el estado actual | Hacerlo | Volver atrás | Riesgo |
|---|---|---|---|---|
| S3: Block Public Access en el bucket de documentos | `aws s3api get-public-access-block --bucket <bucket>` | `put-public-access-block` con las cuatro opciones en `true` | `put-public-access-block` con lo anotado, o `delete-public-access-block` | Bajo: la app usa URLs firmadas |
| RDS: SSL obligatorio | `POSTGRES_SSLMODE` hoy es `prefer` | `POSTGRES_SSLMODE=require` en la task definition y redeploy | Redeploy de la revisión anterior | Bajo: RDS acepta SSL por defecto |
| Retención de logs | `aws logs describe-log-groups --log-group-name-prefix /ecs/ecofilia` | `put-retention-policy` (por ejemplo, 365 días) | `delete-retention-policy` | **Lo que la retención borra no vuelve**: elegir un plazo largo |
| IAM: mínimo privilegio en `ecofiliaTaskRole` | `get-role-policy` / `get-policy-version` y guardar el JSON | Nueva versión de la política | `set-default-policy-version` a la anterior (IAM guarda 5 versiones) | **El más delicado**: un permiso de menos corta S3, SQS, SES o Textract. Hacerlo último, con los logs abiertos |
| WAF en el ALB (opcional) | `aws wafv2 get-web-acl-for-resource` | Asociar una Web ACL en modo *count* primero | `disassociate-web-acl` | Medio: arrancar contando, no bloqueando |

---

## Qué se probó

En local, el 2 de noviembre de 2026, con PostgreSQL 16 y pgvector:

1. Una base migrada con el código de `main` (4e02713), con datos parecidos a los
   reales: la institución `caf`, sus miembros y un admin, staff, `role=admin` sin
   institución, un superuser con MFA, usuarios públicos, operaciones, documentos,
   etiquetas y las skills sembradas.
2. **Ida**, con el código nuevo: las 15 migraciones aplican. Las `caf-*` quedan
   en CAF y el resto en `platform`; CAF tiene su configuración; los secretos MFA
   quedan cifrados.
3. **Nivel 1:** el código de `main` sobre el esquema nuevo crea usuarios, skills,
   instituciones, operaciones y etiquetas, edita usuarios y valida contraseñas.
   Borrar un usuario con auditoría se rechaza (esperado). Sin los defaults en la
   base, crear usuarios, skills e instituciones fallaba: por eso existen
   `user/0014` y `skill/0051`.
4. **Nivel 2:** las migraciones vuelven al estado de `main`. El esquema queda
   idéntico, salvo el nombre interno de una restricción única equivalente, y los
   datos quedan iguales fila por fila, salvo los permisos y tipos de contenido de
   los dos modelos nuevos, que no molestan. El código de `main` escribe sobre esa
   base.
5. **Volver a salir** después de una vuelta atrás: las migraciones aplican de
   nuevo.

El nivel 3 (restaurar un snapshot) no se puede probar en local: es la operación
estándar de RDS. Conviene ensayarlo una vez, restaurando el snapshot del paso 2
a una instancia aparte y borrándola después.
