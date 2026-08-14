"""
Línea base de reproducibilidad de un workflow.

Corre N veces el mismo workflow sobre la misma operación y con los mismos
documentos, y mide en qué se parecen las salidas. Es la métrica que convierte
"a veces contesta distinto" en un número, y sin ella cualquier cambio
posterior al motor es una opinión.

Mide cuatro cosas, de la más objetiva a la más interpretable:

  evidencia          Jaccard del conjunto de fragmentos que recibió cada paso.
                     Si no da 1.00, las corridas ni siquiera vieron lo mismo:
                     cualquier diferencia de redacción es consecuencia de eso
                     y no tiene sentido mirar las otras métricas todavía.

  citas colgadas     Marcadores [#N] que apuntan fuera del rango de fuentes
                     del paso. Es exacto, no heurístico: un [#15] en un paso
                     con 12 fuentes no puede ser correcto.

  fuera de alcance   Documentos de la operación nombrados en el texto de un
                     paso que no están entre las fuentes de ese paso. Detecta
                     menciones literales del nombre completo, así que es una
                     cota inferior: subestima, nunca exagera.

  similitud          Parecido textual entre corridas. Es el proxy más blando —
                     dos redacciones distintas de la misma conclusión son un
                     resultado aceptable, y esta métrica no lo sabe.

Ejemplos:
    # Línea base de tres corridas
    python manage.py workflow_repro_eval --skill-slug caf-iet-datbc-evaluacion-tecnica \\
        --project-id 12 --user-email consultor@ecofilia.site --runs 3 \\
        --out evals/repro_baseline.json

    # Medir sobre ejecuciones que ya existen, sin gastar en modelo
    python manage.py workflow_repro_eval --reuse-executions 340,341,342

    # Comparar una corrida nueva contra la línea base
    python manage.py workflow_repro_eval --skill-slug ... --project-id 12 \\
        --user-email ... --runs 3 --baseline evals/repro_baseline.json
"""
from __future__ import annotations

import itertools
import json
import re
import unicodedata
from difflib import SequenceMatcher

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.project.models import Project
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType
from apps.skill.services import SkillRunner

User = get_user_model()

# Nombres más cortos que esto generan demasiados falsos positivos al buscarlos
# como subcadena ("NDC", "Anexo I") para que el número signifique algo.
MIN_DOC_NAME_CHARS = 12

CITATION_MARKER = re.compile(r"\[#(\d+)\]")


def _normalize(text: str) -> str:
    """Minúsculas, sin acentos y con espacios colapsados, para comparar nombres."""
    lowered = (text or "").lower()
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFD", lowered)
        if unicodedata.category(ch) != "Mn"
    )
    return re.sub(r"\s+", " ", stripped).strip()


def _step_key(entry: dict) -> str:
    return f"{entry.get('step_id')}·{entry.get('title', '')[:40]}"


def _step_text(entry: dict) -> str:
    """Texto del paso, sea prosa o tabla renderizada de forma estable."""
    if entry.get("output_mode") == "table" and entry.get("table"):
        table = entry["table"]
        columns = table.get("columns") or []
        rows = table.get("rows") or []
        return "\n".join(
            " | ".join(str(row.get(col, "")) for col in columns)
            for row in rows if isinstance(row, dict)
        )
    return entry.get("content") or ""


def _sources_set(entry: dict) -> set:
    return {
        (src.get("document_slug"), src.get("chunk_index"))
        for src in entry.get("sources") or []
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pairwise_mean(items: list, score) -> float:
    """Promedio de la métrica sobre todos los pares de corridas."""
    pairs = list(itertools.combinations(items, 2))
    if not pairs:
        return 1.0
    return _mean([score(a, b) for a, b in pairs])


class Command(BaseCommand):
    help = "Mide cuánto varía un workflow entre corridas con el mismo input."

    def add_arguments(self, parser):
        parser.add_argument("--skill-slug", default="", help="Slug del workflow a correr.")
        parser.add_argument("--project-id", type=int, default=0, help="Operación sobre la que correr.")
        parser.add_argument("--user-email", default="", help="Dueño de las ejecuciones.")
        parser.add_argument("--runs", type=int, default=3, help="Cantidad de corridas (default 3).")
        parser.add_argument(
            "--document-slugs", default="",
            help="Acotar el alcance a estos documentos. Vacío = todos los de la operación.",
        )
        parser.add_argument(
            "--reuse-executions", default="",
            help="Medir sobre ids de ejecuciones ya completadas, sin correr nada.",
        )
        parser.add_argument("--out", default="", help="Escribir el resultado como JSON.")
        parser.add_argument("--baseline", default="", help="Comparar contra un JSON previo.")
        parser.add_argument("--yes", action="store_true", help="No pedir confirmación antes de gastar.")

    # -- obtención de las corridas --------------------------------------

    def _reuse(self, raw: str) -> list[SkillExecution]:
        try:
            ids = [int(x) for x in raw.split(",") if x.strip()]
        except ValueError as exc:
            raise CommandError("--reuse-executions espera enteros separados por coma.") from exc
        executions = list(
            SkillExecution.objects.filter(id__in=ids).select_related("skill").order_by("id")
        )
        missing = set(ids) - {e.id for e in executions}
        if missing:
            raise CommandError(f"No existen las ejecuciones: {sorted(missing)}")
        incomplete = [e.id for e in executions if e.status != ExecutionStatus.COMPLETED]
        if incomplete:
            raise CommandError(f"Estas ejecuciones no están completas: {incomplete}")
        return executions

    def _execute(self, options) -> list[SkillExecution]:
        for flag in ("skill_slug", "project_id", "user_email"):
            if not options[flag]:
                raise CommandError(
                    "Sin --reuse-executions hacen falta --skill-slug, --project-id y --user-email."
                )
        try:
            skill = Skill.objects.get(slug=options["skill_slug"])
        except Skill.DoesNotExist as exc:
            raise CommandError(f"No existe el skill '{options['skill_slug']}'.") from exc
        try:
            project = Project.objects.get(id=options["project_id"])
        except Project.DoesNotExist as exc:
            raise CommandError(f"No existe la operación {options['project_id']}.") from exc
        try:
            user = User.objects.get(email=options["user_email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No existe el usuario '{options['user_email']}'.") from exc

        steps = list(skill.steps.all())
        if skill.skill_type == SkillType.COPILOT and any(s.approval_required for s in steps):
            raise CommandError(
                "El workflow tiene pasos con approval_required: la corrida se pausaría "
                "y la medición quedaría sobre un informe parcial. Desactivalos primero."
            )

        slugs = [s.strip() for s in options["document_slugs"].split(",") if s.strip()]
        runs = options["runs"]
        if runs < 2:
            raise CommandError("Con menos de dos corridas no hay nada que comparar.")

        self.stdout.write(
            f"\n{runs} corridas de '{skill.name}' sobre la operación {project.id} "
            f"({len(steps) or 1} pasos cada una)."
        )
        self.stdout.write(self.style.WARNING(
            "Esto consume modelo de verdad. Con Opus, del orden de US$2–3 por corrida."
        ))
        if not options["yes"]:
            answer = input("¿Seguir? [s/N] ").strip().lower()
            if answer not in ("s", "si", "sí", "y", "yes"):
                raise CommandError("Cancelado.")

        executions = []
        for index in range(runs):
            execution = SkillExecution.objects.create(
                skill=skill,
                owner=user,
                project=project,
                metadata={
                    "document_slugs_filter": slugs,
                    "step_document_overrides": {},
                    "review_each_step": False,
                    # Marca para poder encontrarlas y limpiarlas después: son
                    # ejecuciones de prueba conviviendo con las reales.
                    "repro_eval": True,
                },
                status=ExecutionStatus.PENDING,
            )
            self.stdout.write(f"  corrida {index + 1}/{runs} — ejecución {execution.id} … ", ending="")
            self.stdout.flush()
            SkillRunner().run(execution.id)
            execution.refresh_from_db()
            if execution.status != ExecutionStatus.COMPLETED:
                self.stdout.write(self.style.ERROR(f"{execution.status}: {execution.error_message[:120]}"))
                raise CommandError("Una corrida no completó; la línea base sería inválida.")
            self.stdout.write(self.style.SUCCESS("ok"))
            executions.append(execution)
        return executions

    # -- medición -------------------------------------------------------

    def _assert_same_input(self, executions: list[SkillExecution]) -> dict:
        """
        Dos corridas solo son comparables si tuvieron el mismo input.

        Es el manifiesto haciendo su trabajo: sin esta verificación, una
        diferencia causada por un documento reprocesado o por un paso editado
        se leería como falta de determinismo del modelo.
        """
        fingerprints = set()
        doc_states = set()
        for execution in executions:
            manifest = (execution.metadata or {}).get("run_manifest") or {}
            fingerprints.add(manifest.get("definition_fingerprint"))
            snapshot = execution.document_snapshot or []
            doc_states.add(tuple(
                (d.get("id"), d.get("chunk_count"), d.get("last_chunk_id"))
                for d in snapshot
            ))

        if len(fingerprints) > 1:
            raise CommandError(
                "Las corridas usaron definiciones distintas del workflow. "
                "No son comparables: alguien lo editó en el medio."
            )
        if len(doc_states) > 1:
            raise CommandError(
                "Las corridas vieron documentos distintos (alguno se reprocesó). "
                "No son comparables."
            )
        if fingerprints == {None}:
            self.stdout.write(self.style.WARNING(
                "Estas ejecuciones no tienen manifiesto: son anteriores a la "
                "instrumentación. Se miden igual, pero no se puede garantizar "
                "que el input haya sido el mismo."
            ))
        return {
            "definition_fingerprint": next(iter(fingerprints)),
            "documents": executions[0].document_snapshot or [],
        }

    def _steps_by_key(self, execution: SkillExecution) -> dict:
        structured = execution.output_structured or {}
        entries = structured.get("steps") or []
        if entries:
            return {_step_key(e): e for e in entries}
        # Skill de un solo paso: se trata como un workflow de un paso para que
        # el resto de la medición no tenga que distinguir los dos casos.
        return {"quick": {
            "step_id": 0, "title": execution.skill.name,
            "content": execution.output or "",
            "output_mode": execution.output_mode,
            "sources": (execution.metadata or {}).get("sources") or [],
        }}

    def _measure(self, executions: list[SkillExecution], documents: list[dict]) -> dict:
        per_run = [self._steps_by_key(e) for e in executions]
        keys = [k for k in per_run[0] if all(k in run for run in per_run)]
        if not keys:
            raise CommandError("Las corridas no comparten ningún paso; nada que comparar.")

        doc_names = [
            (d["slug"], _normalize(d["name"]))
            for d in documents
            if d.get("name") and len(d["name"]) >= MIN_DOC_NAME_CHARS
        ]

        steps_report = []
        for key in keys:
            entries = [run[key] for run in per_run]

            evidence = _pairwise_mean(
                [_sources_set(e) for e in entries],
                lambda a, b: len(a & b) / len(a | b) if (a | b) else 1.0,
            )
            similarity = _pairwise_mean(
                [_normalize(_step_text(e)) for e in entries],
                lambda a, b: SequenceMatcher(None, a, b).ratio(),
            )

            dangling = 0
            out_of_scope = 0
            for entry in entries:
                sources = entry.get("sources") or []
                text = _step_text(entry)
                dangling += sum(
                    1 for m in CITATION_MARKER.findall(text)
                    if not (1 <= int(m) <= len(sources))
                )
                in_scope = {s.get("document_slug") for s in sources}
                normalized = _normalize(text)
                out_of_scope += sum(
                    1 for slug, name in doc_names
                    if slug not in in_scope and name in normalized
                )

            steps_report.append({
                "step": key,
                "evidence_jaccard": round(evidence, 4),
                "text_similarity": round(similarity, 4),
                "dangling_markers": dangling,
                "out_of_scope_mentions": out_of_scope,
                "sources_per_run": [len(e.get("sources") or []) for e in entries],
            })

        return {
            "runs": [e.id for e in executions],
            "models_used": sorted({
                m
                for e in executions
                for m in ((e.metadata or {}).get("run_manifest") or {}).get("models_used") or []
            }),
            "steps": steps_report,
            "summary": {
                "evidence_jaccard": round(_mean([s["evidence_jaccard"] for s in steps_report]), 4),
                "text_similarity": round(_mean([s["text_similarity"] for s in steps_report]), 4),
                "dangling_markers": sum(s["dangling_markers"] for s in steps_report),
                "out_of_scope_mentions": sum(s["out_of_scope_mentions"] for s in steps_report),
                "steps_compared": len(steps_report),
            },
        }

    # -- ejecución ------------------------------------------------------

    def handle(self, *args, **options):
        executions = (
            self._reuse(options["reuse_executions"])
            if options["reuse_executions"]
            else self._execute(options)
        )
        context = self._assert_same_input(executions)
        report = self._measure(executions, context["documents"])
        report["definition_fingerprint"] = context["definition_fingerprint"]

        self._print(report)

        if options["baseline"]:
            with open(options["baseline"], encoding="utf-8") as handle:
                self._compare(json.load(handle), report)

        if options["out"]:
            with open(options["out"], "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
            self.stdout.write(self.style.SUCCESS(f"\nEscrito en {options['out']}"))

    def _print(self, report: dict) -> None:
        self.stdout.write(f"\n{'evid':>6} {'simil':>6} {'[#]':>4} {'fuera':>6}  paso")
        self.stdout.write("-" * 84)
        for step in report["steps"]:
            marker = "  " if step["evidence_jaccard"] >= 0.999 else "! "
            self.stdout.write(
                f"{marker}{step['evidence_jaccard']:>4.2f} {step['text_similarity']:>6.2f} "
                f"{step['dangling_markers']:>4} {step['out_of_scope_mentions']:>6}  {step['step'][:48]}"
            )

        summary = report["summary"]
        self.stdout.write(f"\nCorridas: {report['runs']}  ·  modelos: {report['models_used'] or ['(sin registro)']}")
        self.stdout.write(f"  estabilidad de evidencia   {summary['evidence_jaccard']:.4f}   (meta 1.0000)")
        self.stdout.write(f"  similitud de texto         {summary['text_similarity']:.4f}   (informativa)")
        self.stdout.write(f"  marcadores [#N] colgados   {summary['dangling_markers']:>6}   (meta 0)")
        self.stdout.write(f"  menciones fuera de alcance {summary['out_of_scope_mentions']:>6}   (meta 0, cota inferior)")

        if summary["evidence_jaccard"] < 0.999:
            self.stdout.write(self.style.WARNING(
                "\nLa evidencia no es estable: las corridas no vieron lo mismo. "
                "Mirar la similitud de texto todavía no dice nada sobre el modelo."
            ))

    def _compare(self, baseline: dict, current: dict) -> None:
        self.stdout.write("\nContra la línea base:")
        if baseline.get("definition_fingerprint") != current.get("definition_fingerprint"):
            self.stdout.write(self.style.WARNING(
                "  La definición del workflow cambió desde la línea base. "
                "La comparación mide el cambio de definición además del de motor."
            ))
        for key, better_is_low in (
            ("evidence_jaccard", False), ("text_similarity", False),
            ("dangling_markers", True), ("out_of_scope_mentions", True),
        ):
            before, after = baseline["summary"].get(key, 0), current["summary"][key]
            delta = after - before
            improved = (delta < 0) if better_is_low else (delta > 0)
            style = self.style.SUCCESS if improved else (
                self.style.WARNING if delta else lambda s: s
            )
            self.stdout.write(f"  {key:<26} {before} → {after}  " + style(f"({delta:+})"))
