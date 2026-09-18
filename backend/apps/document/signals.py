import logging
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.document.models import Document, ChunkingStatus
from apps.document.dispatch import dispatch_processing, mark_pending
from apps.document.tasks import process_document_chunks
from django.conf import settings

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Document)
def handle_document_post_save(sender, instance: Document, created: bool, **kwargs):
    logger.info("Document post_save triggered: id=%s, created=%s", instance.id, created)

    if not created or instance.chunking_done or instance.chunking_status == ChunkingStatus.DONE:
        return

    # Mark as pending so UI knows it's queued. El sello de tiempo es lo que
    # después le permite al reaper distinguir "recién encolado" de "abandonado".
    mark_pending(instance.pk)
    if settings.DEBUG:
        # In debug mode, process immediately (synchronously)
        process_document_chunks(instance.pk)
    else:
        # dispatch_processing, y no `.delay()` pelado: si el encolado falla, el
        # documento tiene que quedar en ERROR y no en un PENDING eterno.
        transaction.on_commit(lambda: dispatch_processing(instance.pk))

