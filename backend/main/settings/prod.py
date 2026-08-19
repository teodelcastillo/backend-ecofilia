from .base import *

ENVIRONMENT_NAME = "Production"

# ALLOWED_HOSTS += ["*"]
STATIC_ROOT = BASE_DIR + "/django-static/"
STATIC_URL = "/django-static/"

import os

# Celery broker: AWS SQS
CELERY_BROKER_URL = "sqs://"

# No result backend (fire-and-forget)
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_STORE_EAGER_RESULT = False

SQS_QUEUE_URL = os.environ.get("SQS_QUEUE_URL")
SQS_REGION = os.environ.get("SQS_REGION", "us-east-2")

if not SQS_QUEUE_URL:
    raise RuntimeError("SQS_QUEUE_URL environment variable is not set")

# Cola dedicada al trabajo interactivo (skills, workflows, evaluaciones).
#
# Sin esta separación todo comparte una sola cola y los mismos slots del worker,
# así que una ingesta de documentos —que puede ocupar un slot 90 minutos— deja a
# un workflow esperando en "pendiente" con una persona mirando la pantalla. SQS
# no tiene prioridades: la única forma de que el lote no mate de hambre a lo
# interactivo es que vayan por colas distintas, con workers distintos.
INTERACTIVE_SQS_QUEUE_URL = os.environ.get("INTERACTIVE_SQS_QUEUE_URL")

CELERY_TASK_DEFAULT_QUEUE = "celery"
CELERY_TASK_INTERACTIVE_QUEUE = "interactive"

_predefined_queues = {
    "celery": {  # name must match CELERY_TASK_DEFAULT_QUEUE
        "url": SQS_QUEUE_URL
    }
}

# El transporte SQS exige que toda cola usada esté declarada acá; si la cola
# interactiva todavía no existe, el ruteo no se aplica y todo sigue yendo a la
# cola por defecto — degradación limpia en vez de tareas que se pierden.
if INTERACTIVE_SQS_QUEUE_URL:
    _predefined_queues[CELERY_TASK_INTERACTIVE_QUEUE] = {
        "url": INTERACTIVE_SQS_QUEUE_URL
    }
    CELERY_TASK_ROUTES = {
        "skill.run": {"queue": CELERY_TASK_INTERACTIVE_QUEUE},
        "evaluation.run": {"queue": CELERY_TASK_INTERACTIVE_QUEUE},
        "evaluation.asg_run": {"queue": CELERY_TASK_INTERACTIVE_QUEUE},
    }

CELERY_BROKER_TRANSPORT_OPTIONS = {
    "region": SQS_REGION,
    "wait_time_seconds": 20,      # long polling
    "visibility_timeout": 3600,   # >= max task runtime
    "predefined_queues": _predefined_queues,
    "queue_name_prefix": ""       # keep names exact
}

# Reliability settings for SQS
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# El reaper de ejecuciones zombis (apps.skill.reliability). `acks_late` ya
# hace que SQS reintente una tarea cuyo worker murió, pero recién a la hora
# (visibility_timeout arriba) y el runner la ignora igual mientras la fila siga
# en `running` — así que sin esto una ejecución muerta se queda "corriendo"
# para siempre y nadie puede reanudarla sin entrar a un shell de producción.
CELERY_BEAT_SCHEDULE = {
    "reap-stalled-skill-executions": {
        "task": "skill.reap_stalled_executions",
        "schedule": 300.0,  # cada 5 minutos
    },
}

# Serialization
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_RESULT_SERIALIZER = "json"

# Timezone
CELERY_ENABLE_UTC = True
TIME_ZONE = "UTC"


INSTALLED_APPS += ["storages"]

AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME")
AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME")
AWS_S3_SIGNATURE_VERSION = "s3v4"
AWS_S3_ADDRESSING_STYLE = "virtual"
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = True
AWS_S3_FILE_OVERWRITE = False

STORAGES = {
    "default": {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"},  # MEDIA
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

MEDIA_URL = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com/"