"""
Management command: diagnostica la cadena de ingesta de documentos.

Recorre, en orden, cada dependencia que ``process_document_chunks`` necesita y
reporta cuál falla. Está pensado para correrse en producción (ECS run-task)
cuando los documentos quedan en ``chunking_status=error`` y el traceback del
worker no alcanza para ubicar la causa.

Uso
---
# Chequeo completo del entorno + ranking de errores guardados en la DB:
python manage.py diagnose_ingestion

# Reproduce las etapas del pipeline sobre un documento concreto (no escribe nada):
python manage.py diagnose_ingestion --doc-id 42

# Sólo el ranking de errores, sin tocar APIs externas:
python manage.py diagnose_ingestion --errors-only

Ninguna variante escribe en la base ni modifica documentos.
"""
from __future__ import annotations

import os
import time
import traceback
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import connection

from apps.document.models import ChunkingStatus, Document

# Longitud a la que se recortan los mensajes de error al agruparlos. Los
# tracebacks de OpenAI/botocore incluyen request-ids distintos por llamada; sin
# recortar, cada fallo idéntico formaría su propio grupo.
_ERROR_BUCKET_CHARS = 160


class Command(BaseCommand):
    help = "Diagnostica la cadena de ingesta de documentos (parsers, APIs, storage, DB)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--doc-id",
            type=int,
            default=None,
            help="Reproduce las etapas del pipeline sobre este documento (sin escribir).",
        )
        parser.add_argument(
            "--errors-only",
            action="store_true",
            default=False,
            help="Sólo agrupa los last_error de la DB; no llama a APIs externas.",
        )
        parser.add_argument(
            "--top",
            type=int,
            default=10,
            help="Cuántos grupos de error mostrar (default: 10).",
        )

    # ── helpers de salida ────────────────────────────────────────────────

    def _section(self, title: str) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"── {title} "))

    def _ok(self, msg: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"  OK    {msg}"))

    def _warn(self, msg: str) -> None:
        self.stdout.write(self.style.WARNING(f"  AVISO {msg}"))

    def _fail(self, msg: str) -> None:
        self.stdout.write(self.style.ERROR(f"  FALLA {msg}"))

    # ── entrypoint ───────────────────────────────────────────────────────

    def handle(self, *args, **options):
        self.failures: list[str] = []

        self.stdout.write(self.style.MIGRATE_HEADING("Diagnóstico de ingesta de documentos"))

        self._check_error_buckets(options["top"])
        self._check_status_counts()

        if not options["errors_only"]:
            self._check_migrations()
            self._check_database()
            self._check_parsers()
            self._check_tiktoken()
            self._check_openai()
            self._check_storage()

        if options["doc_id"] is not None:
            self._replay_document(options["doc_id"])

        self._section("Resumen")
        if self.failures:
            for f in self.failures:
                self._fail(f)
        else:
            self._ok("Sin fallas detectadas en el entorno.")

    # ── 1. errores ya guardados ──────────────────────────────────────────

    def _check_error_buckets(self, top: int) -> None:
        self._section("Errores registrados en Document.last_error")

        rows = (
            Document.objects
            .filter(chunking_status=ChunkingStatus.ERROR)
            .exclude(last_error="")
            .values_list("id", "last_error")
        )
        rows = list(rows)
        if not rows:
            self._ok("Ningún documento en estado 'error' con last_error cargado.")
            return

        buckets: Counter = Counter()
        examples: dict[str, int] = {}
        for doc_id, err in rows:
            key = (err or "").strip()[:_ERROR_BUCKET_CHARS]
            buckets[key] += 1
            examples.setdefault(key, doc_id)

        self.stdout.write(f"  {len(rows)} documentos en error, {len(buckets)} mensajes distintos:")
        self.stdout.write("")
        for msg, count in buckets.most_common(top):
            self.stdout.write(
                self.style.ERROR(f"  [{count:>4}x] ") + f"(ej. doc #{examples[msg]}) {msg}"
            )

    def _check_status_counts(self) -> None:
        self._section("Documentos por chunking_status")
        counts = Counter(
            Document.objects.values_list("chunking_status", flat=True)
        )
        if not counts:
            self._warn("No hay documentos en la base.")
            return
        for status, count in counts.most_common():
            self.stdout.write(f"  {str(status):<12} {count}")

        # Documentos atascados: quedaron en PROCESSING y nadie los retomó.
        stuck = Document.objects.filter(chunking_status=ChunkingStatus.PROCESSING).count()
        if stuck:
            self._warn(
                f"{stuck} documento(s) atascados en 'processing' — el worker murió "
                "a mitad de la tarea (OOM, timeout o redeploy) sin marcar error."
            )

    # ── 2. migraciones ───────────────────────────────────────────────────

    def _check_migrations(self) -> None:
        self._section("Migraciones")
        try:
            from django.db.migrations.executor import MigrationExecutor

            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if plan:
                self._fail(f"{len(plan)} migración(es) sin aplicar:")
                for migration, _backwards in plan:
                    self.stdout.write(f"        {migration.app_label}.{migration.name}")
                self.failures.append(
                    "Hay migraciones sin aplicar — corré `manage.py migrate` en ECS."
                )
            else:
                self._ok("Todas las migraciones están aplicadas.")
        except Exception as exc:
            self._fail(f"No se pudo calcular el plan de migraciones: {exc!r}")
            self.failures.append("El grafo de migraciones no se puede resolver.")

    # ── 3. base de datos ─────────────────────────────────────────────────

    def _check_database(self) -> None:
        self._section("PostgreSQL / pgvector")
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT extname FROM pg_extension")
                exts = {r[0] for r in cur.fetchall()}
                for needed in ("vector", "unaccent"):
                    if needed in exts:
                        self._ok(f"extensión '{needed}' instalada.")
                    else:
                        self._fail(f"falta la extensión '{needed}'.")
                        self.failures.append(f"Extensión PostgreSQL '{needed}' ausente.")

                # La columna generada content_norm depende de esta función; si no
                # existe, todo INSERT de SmartChunk falla.
                cur.execute("SELECT to_regproc('immutable_unaccent(text)') IS NOT NULL")
                if cur.fetchone()[0]:
                    self._ok("función immutable_unaccent(text) presente.")
                else:
                    self._fail("falta la función immutable_unaccent(text).")
                    self.failures.append(
                        "immutable_unaccent(text) no existe — los INSERT de SmartChunk fallan."
                    )

                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'document_smartchunk'"
                )
                cols = {r[0] for r in cur.fetchall()}
                for needed in ("page_number", "context_summary", "content_norm", "embedding"):
                    if needed in cols:
                        self._ok(f"columna smartchunk.{needed} presente.")
                    else:
                        self._fail(f"falta la columna smartchunk.{needed}.")
                        self.failures.append(f"Columna smartchunk.{needed} ausente.")
        except Exception as exc:
            self._fail(f"Error consultando la base: {exc!r}")
            self.failures.append("No se pudo inspeccionar el esquema de PostgreSQL.")

    # ── 4. parsers ───────────────────────────────────────────────────────

    def _check_parsers(self) -> None:
        self._section("Librerías de parseo")

        try:
            import fitz  # noqa: F401  (PyMuPDF)

            self._ok(f"PyMuPDF (fitz) disponible — versión {getattr(fitz, '__version__', '?')}.")
        except Exception as exc:
            self._fail(
                f"PyMuPDF (fitz) NO disponible: {exc!r}. "
                "Todos los PDF caen al fallback PyPDF2: sin marcadores de página "
                "(page_number queda NULL) y sin recorte de encabezados/pies."
            )
            self.failures.append(
                "PyMuPDF no está instalado — el parseo de PDF corre degradado."
            )

        for mod, label in (("PyPDF2", "PyPDF2 (fallback PDF)"), ("docx", "python-docx (DOCX)")):
            try:
                __import__(mod)
                self._ok(f"{label} disponible.")
            except Exception as exc:
                self._fail(f"{label} NO disponible: {exc!r}")
                self.failures.append(f"{label} no está instalado.")

    # ── 5. tiktoken ──────────────────────────────────────────────────────

    def _check_tiktoken(self) -> None:
        self._section("tiktoken (conteo de tokens)")
        cache_dir = os.environ.get("TIKTOKEN_CACHE_DIR")
        self.stdout.write(f"  TIKTOKEN_CACHE_DIR={cache_dir or '(sin definir → /tmp/data-gym-cache)'}")
        try:
            from apps.document.utils.client_tiktoken import token_count

            started = time.time()
            n = token_count("prueba de conteo de tokens")
            elapsed = time.time() - started
            self._ok(f"encoding cargado en {elapsed:.2f}s (token_count={n}).")
            if elapsed > 1.0:
                self._warn(
                    "La carga tardó más de 1s: el vocabulario se descargó de "
                    "openaipublic.blob.core.windows.net en vez de leerse de la caché. "
                    "Si el egress a ese host se corta, TODA la ingesta falla."
                )
        except Exception as exc:
            self._fail(f"tiktoken no pudo cargar el encoding: {exc!r}")
            self.failures.append(
                "tiktoken no puede cargar cl100k_base — sin egress a "
                "openaipublic.blob.core.windows.net toda la ingesta falla."
            )

    # ── 6. OpenAI ────────────────────────────────────────────────────────

    def _check_openai(self) -> None:
        self._section("OpenAI (embeddings + completions)")

        if not os.environ.get("OPENAI_API_KEY"):
            self._fail("OPENAI_API_KEY no está definida.")
            self.failures.append("OPENAI_API_KEY ausente — ningún documento puede embeberse.")
            return
        self._ok("OPENAI_API_KEY definida.")

        from apps.document.utils.client_openia import MODEL_EMBEDDING, embed_text

        self.stdout.write(f"  MODEL_EMBEDDING={MODEL_EMBEDDING}")
        self.stdout.write(
            f"  EMBEDDING_DIMENSIONS={os.environ.get('EMBEDDING_DIMENSIONS') or '(nativo)'}"
        )
        try:
            started = time.time()
            vec = embed_text("prueba de embedding")
            elapsed = time.time() - started
            self._ok(f"embed_text() respondió en {elapsed:.2f}s — {len(vec)} dimensiones.")
            if len(vec) != 1536:
                self._fail(
                    f"El modelo devuelve {len(vec)} dimensiones pero SmartChunk.embedding "
                    "está declarado con 1536. Todo INSERT de chunk va a fallar."
                )
                self.failures.append(
                    f"Dimensión de embedding ({len(vec)}) != 1536 declarada en el modelo."
                )
        except Exception as exc:
            self._fail(f"embed_text() falló: {exc!r}")
            self.failures.append(
                "La API de embeddings de OpenAI no responde (clave inválida, cuota "
                "agotada o egress bloqueado) — ningún documento puede procesarse."
            )

    # ── 7. storage ───────────────────────────────────────────────────────

    def _check_storage(self) -> None:
        self._section("Storage de archivos (S3)")
        try:
            from django.core.files.storage import default_storage

            self.stdout.write(f"  backend={type(default_storage).__name__}")
            self.stdout.write(
                f"  AWS_STORAGE_BUCKET_NAME={os.environ.get('AWS_STORAGE_BUCKET_NAME') or '(sin definir)'}"
            )

            doc = (
                Document.objects
                .exclude(file="")
                .exclude(file=None)
                .order_by("-created_at")
                .first()
            )
            if not doc:
                self._warn("No hay documentos con archivo para probar la lectura.")
                return

            started = time.time()
            with doc.file.open("rb") as fh:
                head = fh.read(1024)
            elapsed = time.time() - started
            self._ok(
                f"lectura de '{doc.file.name}' (doc #{doc.id}) OK — "
                f"{len(head)} bytes en {elapsed:.2f}s."
            )
        except Exception as exc:
            self._fail(f"No se pudo leer el archivo desde el storage: {exc!r}")
            self.failures.append(
                "El worker no puede descargar los archivos del bucket (permisos del "
                "task role, bucket mal configurado o egress a S3 bloqueado)."
            )

    # ── 8. replay de un documento ────────────────────────────────────────

    def _replay_document(self, doc_id: int) -> None:
        self._section(f"Replay del pipeline sobre el documento #{doc_id} (sin escribir)")

        try:
            doc = Document.objects.get(pk=doc_id)
        except Document.DoesNotExist:
            self._fail(f"No existe el documento #{doc_id}.")
            self.failures.append(f"Documento #{doc_id} inexistente.")
            return

        self.stdout.write(f"  nombre={doc.name!r}")
        self.stdout.write(f"  archivo={doc.file.name if doc.file else '(sin archivo)'}")
        self.stdout.write(f"  estado={doc.chunking_status} last_error={doc.last_error[:200]!r}")

        if not doc.file:
            self._fail("El documento no tiene archivo adjunto.")
            self.failures.append(f"Documento #{doc_id} sin archivo.")
            return

        import tempfile

        from apps.document.utils.parser import parse_file

        ext = os.path.splitext(doc.file.name or "")[1] or os.path.splitext(doc.name or "")[1]
        self.stdout.write(f"  extensión detectada={ext!r}")

        tmp_path = None
        try:
            started = time.time()
            with doc.file.open("rb") as src, tempfile.NamedTemporaryFile(
                suffix=ext or "", delete=False
            ) as dst:
                tmp_path = dst.name
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(block)
            size = os.path.getsize(tmp_path)
            self._ok(f"descarga OK — {size} bytes en {time.time() - started:.2f}s.")

            started = time.time()
            text = parse_file(tmp_path) or ""
            self._ok(
                f"parseo OK — {len(text)} caracteres en {time.time() - started:.2f}s."
            )
            if not text.strip():
                self._fail(
                    "El parser devolvió texto vacío: acá es donde el documento queda "
                    "en error. PDF escaneado (sólo imágenes), protegido, o PyPDF2 "
                    "no pudo extraer el contenido."
                )
                self.failures.append(f"Documento #{doc_id}: el parser no extrae texto.")
                return

            marker_count = text.count("<<<PAGE:")
            if marker_count:
                self._ok(f"{marker_count} marcadores de página detectados.")
            else:
                self._warn(
                    "Sin marcadores <<<PAGE:N>>> — los chunks van a quedar con "
                    "page_number NULL (síntoma típico de PyMuPDF ausente en PDFs)."
                )

            self.stdout.write("  primeros 300 caracteres extraídos:")
            self.stdout.write(f"    {text[:300]!r}")

            from apps.document.utils.chunker import _semantic_paragraphs

            started = time.time()
            segments = _semantic_paragraphs(text)
            self._ok(
                f"segmentación OK — {len(segments)} segmentos en "
                f"{time.time() - started:.2f}s (sin llamadas al LLM)."
            )
            if not segments:
                self._fail(
                    "El chunker no produjo ningún segmento: el documento se guardaría "
                    "sin chunks y no sería recuperable por RAG."
                )
                self.failures.append(f"Documento #{doc_id}: 0 segmentos tras el chunking.")
            else:
                self.stdout.write(
                    f"  el pipeline real haría {len(segments)} llamadas a "
                    "generate_chunk_context + otras tantas a embed_text."
                )
        except Exception:
            self._fail("Excepción durante el replay:")
            for line in traceback.format_exc().splitlines():
                self.stdout.write(f"        {line}")
            self.failures.append(f"Documento #{doc_id}: el replay lanzó excepción.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
