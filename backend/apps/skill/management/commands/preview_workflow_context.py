"""
Qué base documental recibe cada paso de un workflow, sin ejecutarlo.

Hasta ahora la única manera de saber qué veía el modelo era correr el workflow
entero y leer los diagnósticos después: diez minutos y el costo de diecisiete
llamadas para responder una pregunta sobre el input. Peor, si la respuesta era
"este paso no recibió nada", ya había un informe escrito sobre esa nada.

El cálculo vive en ``apps.skill.context_preview`` porque el builder lo necesita
también; este comando es una de sus dos vistas. Ninguna de las dos llama al
modelo ni escribe en la base.

    # Panorama: un renglón por paso
    python manage.py preview_workflow_context --skill-id 46 --project-id 34

    # El prompt completo de un paso, tal como viaja
    python manage.py preview_workflow_context --skill-id 46 --project-id 34 \\
        --step 7 --dump
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.skill import context_budget
from apps.skill.context_preview import build_preview, simulated_execution
from apps.skill.models import Skill, SkillExecution


class Command(BaseCommand):
    help = "Muestra la base documental que recibiría cada paso de un workflow."

    def add_arguments(self, parser):
        parser.add_argument("--skill-id", type=int, required=True)
        parser.add_argument(
            "--project-id", type=int,
            help="Operación sobre la que se simula. Alternativa: --execution-id.",
        )
        parser.add_argument(
            "--execution-id", type=int,
            help="Reusa el alcance y los parámetros de una corrida existente.",
        )
        parser.add_argument(
            "--step", type=int, default=0,
            help="Posición del paso a inspeccionar en detalle. 0 = todos, resumidos.",
        )
        parser.add_argument(
            "--dump", action="store_true",
            help="Imprimir el corpus completo del paso elegido. Es largo.",
        )
        parser.add_argument(
            "--no-retrieval", action="store_true",
            help="No buscar fragmentos en los documentos degradados (más rápido).",
        )

    def handle(self, *args, **options):
        skill = Skill.objects.filter(id=options["skill_id"]).first()
        if skill is None:
            raise CommandError(f"No existe la skill {options['skill_id']}.")

        execution = self._execution(skill, options)
        positions = [options["step"]] if options["step"] else None

        try:
            preview = build_preview(
                skill,
                execution.project,
                execution=execution,
                positions=positions,
                measure_fragments=not options["no_retrieval"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        window = preview.window
        self.stdout.write("")
        self.stdout.write(f"Workflow  : [{skill.slug}] {skill.name}")
        self.stdout.write(
            f"Operación : {preview.project['name']} "
            f"({preview.project['documents']} documentos)"
        )
        self.stdout.write(
            f"Ventana   : {window['context_window']:,} · colchón "
            f"{window['safety_margin']:,} · salida {window['output_reserve']:,}"
        )
        self.stdout.write(f"System    : {window['system_tokens']:,} tokens")
        self.stdout.write("")

        header = (
            f"{'paso':>4}  {'reserva':>9}  {'cacheable':>9}  {'variable':>9}  "
            f"{'total':>9}  {'modelo':<18}  documentos"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for step in preview.steps:
            variable = (
                f"{step.variable_tokens:>9,}"
                if step.variable_tokens is not None
                else f"{'—':>9}"
            )
            if not step.reads_documents:
                detail = "(sólo pasos previos)"
            else:
                detail = ", ".join(
                    f"{d.slug}"
                    + {
                        context_budget.FULL: "",
                        context_budget.PARTIAL: " (fragmentos)",
                        context_budget.UNAVAILABLE: " (SIN TEXTO)",
                    }[d.mode]
                    for d in step.documents
                )
            self.stdout.write(
                f"{step.position:>4}  {step.reserved_tokens:>9,}  "
                f"{step.cacheable_tokens:>9,}  {variable}  "
                f"{step.total_tokens:>9,}  {step.model:<18}  {detail}"
            )

        self.stdout.write("")
        if preview.warnings:
            self.stdout.write(self.style.ERROR("Avisos:"))
            for line in preview.warnings:
                self.stdout.write(f"  {line}")
        else:
            self.stdout.write(
                self.style.SUCCESS("Ningún paso excede la ventana de contexto.")
            )

        self._report_cache(preview.cache)

        if options["dump"] and positions:
            self._dump(skill, execution, positions[0], options)

        self.stdout.write(
            "\nNota: sin secciones previas, que en la corrida real ocupan "
            "presupuesto. Estos números son el techo, no la corrida."
        )

    def _report_cache(self, cache: dict) -> None:
        self.stdout.write("")
        if not cache.get("measurable"):
            reason = cache.get("reason")
            if reason == "unstable_prefix":
                self.stdout.write(
                    self.style.ERROR(
                        "La parte cacheable NO es idéntica entre pasos "
                        f"({len(cache['distinct_sizes'])} tamaños distintos): la "
                        "caché se invalida y el corpus se paga una vez por paso."
                    )
                )
            elif reason == "single_step":
                self.stdout.write(
                    f"Parte cacheable: {cache['cacheable_tokens']:,} tokens. Con un "
                    "solo paso no hay ahorro que medir — corré sin --step para el "
                    "número real."
                )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Parte cacheable idéntica en los {cache['steps']} pasos: "
                f"{cache['cacheable_tokens']:,} tokens."
            )
        )
        self.stdout.write(
            f"  entrada facturable con caché:  {cache['billable_with_cache']:>12,} tokens\n"
            f"  entrada facturable sin caché:  {cache['billable_without_cache']:>12,} tokens\n"
            f"  ahorro:                        {cache['savings_ratio'] * 100:>11.0f}%"
        )

    def _dump(self, skill, execution, position: int, options) -> None:
        """El corpus completo del paso elegido, tal como viaja."""
        from apps.skill.services import (
            _resolve_step_documents,
            _with_operation_context,
            build_step_corpus,
            resolve_documents,
        )

        step = next((s for s in skill.steps.all() if s.position == position), None)
        if step is None:
            return
        documents = resolve_documents(execution)
        system_prompt = _with_operation_context(skill.system_prompt, execution)
        reserved = (
            context_budget.estimate_tokens(system_prompt)
            + context_budget.estimate_tokens(f"{step.title}\n{step.instructions}")
            + context_budget.output_reserve()
        )
        corpus = build_step_corpus(
            execution=execution,
            step_documents=_resolve_step_documents(step, documents, []),
            query_text=f"{step.title}. {step.instructions}".strip(),
            reserved_tokens=reserved,
            blueprint_id=getattr(execution.project, "blueprint_document_id", None),
            document_texts={},
            retrieve_partials=not options["no_retrieval"],
        )
        parts = [corpus.inventory] + [d.text for d in corpus.documents]
        if corpus.volatile:
            parts.append(corpus.volatile)
        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write("\n\n".join(p for p in parts if p))
        self.stdout.write("=" * 72)

    def _execution(self, skill, options) -> SkillExecution:
        if options.get("execution_id"):
            execution = SkillExecution.objects.filter(id=options["execution_id"]).first()
            if execution is None:
                raise CommandError(f"No existe la ejecución {options['execution_id']}.")
            return execution
        if not options.get("project_id"):
            raise CommandError("Hace falta --project-id o --execution-id.")
        from apps.project.models import Project

        project = Project.objects.filter(id=options["project_id"]).first()
        if project is None:
            raise CommandError(f"No existe la operación {options['project_id']}.")
        try:
            return simulated_execution(skill, project)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
