"""
Qué base documental recibe cada paso de un workflow, sin ejecutarlo.

Hasta ahora la única manera de saber qué veía el modelo era correr el workflow
entero y leer los diagnósticos después: diez minutos y el costo de diecisiete
llamadas para responder una pregunta sobre el input. Peor, si la respuesta era
"este paso no recibió nada", ya había un informe escrito sobre esa nada.

Este comando arma exactamente el mismo corpus que armaría la corrida —usa
``build_step_corpus``, no una reimplementación— y no llama al modelo ni escribe
en la base.

    # Panorama: un renglón por paso
    python manage.py preview_workflow_context --skill-id 46 --project-id 34

    # El prompt completo de un paso, tal como viaja
    python manage.py preview_workflow_context --skill-id 46 --project-id 34 \\
        --step 7 --dump
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.skill import context_budget
from apps.skill.models import Skill, SkillExecution
from apps.skill.services import (
    build_step_corpus,
    resolve_documents,
    _resolve_step_documents,
    _with_operation_context,
)


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
        documents = resolve_documents(execution)
        steps = list(skill.steps.all())
        if not steps:
            raise CommandError("El workflow no tiene pasos definidos.")

        blueprint_id = getattr(execution.project, "blueprint_document_id", None)
        document_texts: dict[int, str] = {}
        system_prompt = _with_operation_context(skill.system_prompt, execution)
        system_tokens = context_budget.estimate_tokens(system_prompt)
        output_reserve = context_budget.output_reserve()

        self.stdout.write("")
        self.stdout.write(f"Workflow  : [{skill.slug}] {skill.name}")
        self.stdout.write(f"Operación : {execution.project} ({documents.count()} documentos)")
        self.stdout.write(
            f"Ventana   : {context_budget.CONTEXT_WINDOW:,} · colchón "
            f"{context_budget.CONTEXT_SAFETY_MARGIN:,} · salida {output_reserve:,}"
        )
        self.stdout.write(f"System    : {system_tokens:,} tokens")
        self.stdout.write("")

        target = options["step"]
        header = f"{'paso':>4}  {'reserva':>9}  {'corpus':>9}  {'total':>9}  documentos"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        overflow: list[str] = []
        for step in steps:
            if target and step.position != target:
                continue
            step_documents = _resolve_step_documents(step, documents, [])
            # La reserva del paso sin las secciones previas: acá no hay corrida,
            # así que no existen. Es el mejor caso; en la corrida real el
            # historial come presupuesto y algún documento más puede degradarse.
            step_tokens = context_budget.estimate_tokens(
                f"{step.title}\n{step.instructions}"
            )
            reserved = system_tokens + step_tokens + output_reserve

            corpus, chunks, plan = build_step_corpus(
                execution=execution,
                step_documents=step_documents,
                query_text=f"{step.title}. {step.instructions}".strip(),
                reserved_tokens=reserved,
                blueprint_id=blueprint_id,
                document_texts=document_texts,
                retrieve_partials=not options["no_retrieval"],
            )
            corpus_tokens = context_budget.estimate_tokens(corpus)
            total = reserved + corpus_tokens

            detail = []
            for delivery in plan.deliveries:
                mark = {
                    context_budget.FULL: "",
                    context_budget.PARTIAL: " (fragmentos)",
                    context_budget.UNAVAILABLE: " (SIN TEXTO)",
                }[delivery.mode]
                detail.append(f"{delivery.slug}{mark}")
            self.stdout.write(
                f"{step.position:>4}  {reserved:>9,}  {corpus_tokens:>9,}  "
                f"{total:>9,}  {', '.join(detail)}"
            )
            if total > context_budget.CONTEXT_WINDOW:
                overflow.append(f"paso {step.position}: {total:,} tokens")

            if options["dump"] and target and step.position == target:
                self.stdout.write("")
                self.stdout.write("=" * 72)
                self.stdout.write(corpus)
                self.stdout.write("=" * 72)

        self.stdout.write("")
        if overflow:
            self.stdout.write(self.style.ERROR("Pasos que exceden la ventana:"))
            for line in overflow:
                self.stdout.write(f"  {line}")
        else:
            self.stdout.write(
                self.style.SUCCESS("Ningún paso excede la ventana de contexto.")
            )
        self.stdout.write(
            "\nNota: sin secciones previas, que en la corrida real ocupan "
            "presupuesto. Estos números son el techo, no la corrida."
        )

    def _execution(self, skill, options) -> SkillExecution:
        """Una ejecución para simular, nunca persistida."""
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
        # Sin `save()`: el comando no deja rastro en el historial de corridas.
        return SkillExecution(
            skill=skill, owner=skill.owner, project=project, metadata={}
        )
