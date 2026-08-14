"""
Cuánto pesa, en tokens, la base documental de cada operación.

Es la medición que decide la arquitectura del motor de workflows. Si el corpus
de una operación entra cómodo en la ventana de contexto, entonces recuperar
fragmentos deja de ser necesario: se le pueden pasar los documentos enteros al
modelo, y cada decisión que hoy toma el RAG sobre *qué mostrar* —y que puede
resolver distinto en cada corrida— desaparece en vez de mitigarse. Si no
entra, la recuperación sigue siendo el camino principal y merece la inversión.

Por defecto no cuenta token por token: calibra la relación caracteres/token
contra la API sobre una muestra y extrapola. Es órdenes de magnitud más rápido
y para decidir una arquitectura alcanza de sobra. `--exact` cuenta todo.

Ejemplos:
    # Panorama de todas las operaciones
    python manage.py measure_operation_corpus

    # Solo algunas, con conteo exacto y salida a archivo
    python manage.py measure_operation_corpus --project-ids 12,15 --exact \\
        --out evals/corpus.json
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db.models.functions import Length

from apps.document.models import ChunkingStatus, Document
from apps.project.models import Project

# Ventana de Opus 5. El presupuesto para documentos es menor que la ventana:
# hay que dejar lugar para el prompt del paso, las secciones previas y la
# salida, que en un entregable largo no es despreciable.
CONTEXT_WINDOW = 1_000_000
COMFORTABLE = 400_000   # entra sin pensarlo
TIGHT = 800_000         # entra, pero hay que administrar el alcance por paso

# Precios de lista de Opus 5, por millón de tokens. Solo entrada: la salida no
# depende del tamaño del corpus.
USD_PER_MTOK_INPUT = 5.0
USD_PER_MTOK_CACHE_WRITE_1H = 10.0   # 2x sobre entrada, TTL de 1 hora
USD_PER_MTOK_CACHE_READ = 0.5        # 0.1x sobre entrada

# Porción de cada documento que se manda a contar durante la calibración.
CALIBRATION_SLICE_CHARS = 20_000
# La API acepta pedidos grandes, pero un documento entero puede ser de varios
# megabytes: en modo exacto se cuenta por tramos y se suman.
EXACT_SLICE_CHARS = 400_000


@dataclass
class OperationCorpus:
    project_id: int
    name: str
    doc_count: int
    chars: int
    tokens: int = 0
    has_blueprint: bool = False
    docs_sin_texto: list = field(default_factory=list)
    docs_incompletos: list = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.tokens <= COMFORTABLE:
            return "contexto-primero"
        if self.tokens <= TIGHT:
            return "contexto-primero (acotado)"
        return "RAG"


def _percentile(values: list[int], pct: float) -> int:
    """Percentil por índice sobre la lista ordenada. Sin interpolar: son pocos datos."""
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


class Command(BaseCommand):
    help = "Mide en tokens la base documental de cada operación y recomienda arquitectura."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project-ids", default="",
            help="Ids de operación separados por coma. Vacío = todas las que tengan documentos.",
        )
        parser.add_argument(
            "--model", default=os.environ.get("LLM_MODEL_DEEP") or "claude-opus-5",
            help="Modelo contra el que contar (los tokenizadores difieren entre modelos).",
        )
        parser.add_argument(
            "--sample-docs", type=int, default=12,
            help="Documentos usados para calibrar caracteres/token. Ignorado con --exact.",
        )
        parser.add_argument(
            "--exact", action="store_true",
            help="Contar cada documento contra la API en vez de extrapolar. Lento.",
        )
        parser.add_argument(
            "--steps", type=int, default=18,
            help="Pasos del workflow, para estimar el costo de entrada por corrida.",
        )
        parser.add_argument("--out", default="", help="Escribir el detalle como JSON.")

    # -- conteo ---------------------------------------------------------

    def _client(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - guarda de dependencia
            raise CommandError("Falta el paquete 'anthropic'.") from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise CommandError(
                "ANTHROPIC_API_KEY no está seteada. Sin ella no se puede contar "
                "tokens contra el tokenizador real."
            )
        return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def _count(self, client, model: str, text: str) -> int:
        """Tokens de ``text``. Por tramos: un documento entero puede ser enorme."""
        total = 0
        for start in range(0, len(text), EXACT_SLICE_CHARS):
            piece = text[start:start + EXACT_SLICE_CHARS]
            if not piece.strip():
                continue
            response = client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": piece}],
            )
            total += response.input_tokens
        return total

    def _calibrate(self, client, model: str, sample_docs: int) -> float:
        """
        Caracteres por token, medido sobre una muestra real del corpus.

        Devuelve el promedio ponderado por caracteres: un documento largo pesa
        más que uno corto, que es como se comporta el total que queremos estimar.
        """
        sample = list(
            Document.objects
            .exclude(extracted_text="")
            .annotate(chars=Length("extracted_text"))
            .filter(chars__gte=2000)
            .order_by("-chars")
            .values_list("extracted_text", flat=True)[:sample_docs]
        )
        if not sample:
            raise CommandError("No hay documentos con texto extraído para calibrar.")

        total_chars = 0
        total_tokens = 0
        for text in sample:
            piece = text[:CALIBRATION_SLICE_CHARS]
            tokens = self._count(client, model, piece)
            if tokens:
                total_chars += len(piece)
                total_tokens += tokens
        if not total_tokens:
            raise CommandError("La calibración no devolvió tokens; revisar la API key.")

        ratio = total_chars / total_tokens
        self.stdout.write(
            f"Calibración sobre {len(sample)} documentos: "
            f"{ratio:.2f} caracteres por token ({total_chars:,} chars / {total_tokens:,} tokens)\n"
        )
        return ratio

    # -- ejecución ------------------------------------------------------

    def handle(self, *args, **options):
        projects = Project.objects.all().order_by("id")
        if options["project_ids"]:
            try:
                ids = [int(x) for x in options["project_ids"].split(",") if x.strip()]
            except ValueError as exc:
                raise CommandError("--project-ids espera enteros separados por coma.") from exc
            projects = projects.filter(id__in=ids)

        client = self._client()
        model = options["model"]
        ratio = None if options["exact"] else self._calibrate(
            client, model, options["sample_docs"]
        )

        results: list[OperationCorpus] = []
        for project in projects.select_related("blueprint_document"):
            docs = list(
                Document.objects
                .filter(projects__id=project.id)
                .annotate(chars=Length("extracted_text"))
                .values("id", "name", "chars", "chunking_status")
                .order_by("id")
            )
            if not docs:
                continue

            corpus = OperationCorpus(
                project_id=project.id,
                name=project.name,
                doc_count=len(docs),
                chars=sum(d["chars"] or 0 for d in docs),
                has_blueprint=project.blueprint_document_id is not None,
            )
            for doc in docs:
                if not doc["chars"]:
                    corpus.docs_sin_texto.append(doc["name"])
                elif doc["chunking_status"] != ChunkingStatus.DONE:
                    corpus.docs_incompletos.append(f"{doc['name']} ({doc['chunking_status']})")

            if options["exact"]:
                texts = Document.objects.filter(
                    id__in=[d["id"] for d in docs]
                ).values_list("extracted_text", flat=True)
                corpus.tokens = sum(self._count(client, model, t or "") for t in texts)
            else:
                corpus.tokens = int(corpus.chars / ratio) if ratio else 0

            results.append(corpus)

        if not results:
            raise CommandError("Ninguna operación tiene documentos asociados.")

        self._report(results, steps=options["steps"], model=model, exact=options["exact"])

        if options["out"]:
            payload = {
                "model": model,
                "exact": options["exact"],
                "chars_per_token": ratio,
                "operations": [vars(r) | {"verdict": r.verdict} for r in results],
            }
            with open(options["out"], "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS(f"\nDetalle escrito en {options['out']}"))

    def _report(self, results: list[OperationCorpus], *, steps: int, model: str, exact: bool) -> None:
        results.sort(key=lambda r: r.tokens, reverse=True)

        self.stdout.write(
            f"\n{'id':>5}  {'docs':>5}  {'tokens':>10}  {'veredicto':<28}  operación"
        )
        self.stdout.write("-" * 96)
        for row in results:
            flag = " ⚠" if (row.docs_sin_texto or row.docs_incompletos) else ""
            self.stdout.write(
                f"{row.project_id:>5}  {row.doc_count:>5}  {row.tokens:>10,}  "
                f"{row.verdict:<28}  {row.name[:36]}{flag}"
            )

        tokens = [r.tokens for r in results]
        p50, p90, top = _percentile(tokens, 50), _percentile(tokens, 90), max(tokens)

        self.stdout.write(f"\nOperaciones medidas: {len(results)}  ·  modelo: {model}"
                          f"  ·  conteo: {'exacto' if exact else 'calibrado'}")
        self.stdout.write(f"  P50 {p50:>10,} tokens")
        self.stdout.write(f"  P90 {p90:>10,} tokens")
        self.stdout.write(f"  máx {top:>10,} tokens   (ventana: {CONTEXT_WINDOW:,})")

        # El P90 es el que manda: la arquitectura tiene que sostener el caso
        # difícil, no el mediano.
        self.stdout.write("\nCompuerta arquitectónica (según P90):")
        if p90 <= COMFORTABLE:
            self.stdout.write(self.style.SUCCESS(
                "  Contexto-primero. El corpus del 90% de las operaciones entra sin "
                "administrar nada; la recuperación queda como plan B."
            ))
        elif p90 <= TIGHT:
            self.stdout.write(self.style.WARNING(
                "  Contexto-primero acotado por paso. Entra, pero conviene que cada "
                "paso reciba solo los documentos de su alcance en vez de todo el corpus."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "  RAG sigue siendo el camino principal. El corpus del caso difícil no "
                "entra: la Fase 2 merece la inversión completa."
            ))

        # Costo de entrada de una corrida sobre la operación mediana, con el
        # prefijo cacheado a una hora: se escribe una vez y se lee en cada paso.
        write = p50 / 1_000_000 * USD_PER_MTOK_CACHE_WRITE_1H
        read = (max(steps - 1, 0)) * p50 / 1_000_000 * USD_PER_MTOK_CACHE_READ
        naive = steps * p50 / 1_000_000 * USD_PER_MTOK_INPUT
        self.stdout.write(
            f"\nCosto de entrada por corrida ({steps} pasos, operación P50, precios de lista):"
        )
        self.stdout.write(f"  con caché de 1 h : ~US$ {write + read:.2f}")
        self.stdout.write(f"  sin caché        : ~US$ {naive:.2f}")

        con_faltantes = [r for r in results if r.docs_sin_texto or r.docs_incompletos]
        if con_faltantes:
            self.stdout.write(self.style.WARNING(
                f"\n{len(con_faltantes)} operaciones tienen documentos sin texto o "
                "incompletos. Contexto-primero los deja fuera igual que el RAG:"
            ))
            for row in con_faltantes[:10]:
                detail = ", ".join((row.docs_sin_texto + row.docs_incompletos)[:3])
                self.stdout.write(f"  [{row.project_id}] {row.name[:32]}: {detail}")
