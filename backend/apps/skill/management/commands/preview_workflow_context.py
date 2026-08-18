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
        header = (
            f"{'paso':>4}  {'reserva':>9}  {'cacheable':>9}  {'variable':>9}  "
            f"{'total':>9}  documentos"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        overflow: list[str] = []
        # Para el reporte de caché: lo estable se paga una vez, lo demás en
        # cada paso. Es la diferencia entre una corrida de 4 dólares y una de 19.
        cached_tokens: list[int] = []
        uncached_tokens: list[int] = []
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

            stable, volatile, chunks, plan = build_step_corpus(
                execution=execution,
                step_documents=step_documents,
                query_text=f"{step.title}. {step.instructions}".strip(),
                reserved_tokens=reserved,
                blueprint_id=blueprint_id,
                document_texts=document_texts,
                retrieve_partials=not options["no_retrieval"],
            )
            corpus = "\n\n".join(p for p in (stable, volatile) if p)
            stable_tokens = context_budget.estimate_tokens(stable)
            volatile_tokens = context_budget.estimate_tokens(volatile)
            corpus_tokens = stable_tokens + volatile_tokens
            total = reserved + corpus_tokens
            cached_tokens.append(stable_tokens)
            uncached_tokens.append(reserved + volatile_tokens)

            detail = []
            for delivery in plan.deliveries:
                mark = {
                    context_budget.FULL: "",
                    context_budget.PARTIAL: " (fragmentos)",
                    context_budget.UNAVAILABLE: " (SIN TEXTO)",
                }[delivery.mode]
                detail.append(f"{delivery.slug}{mark}")
            self.stdout.write(
                f"{step.position:>4}  {reserved:>9,}  {stable_tokens:>9,}  "
                f"{volatile_tokens:>9,}  {total:>9,}  {', '.join(detail)}"
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
        self._report_cache(cached_tokens, uncached_tokens)
        self.stdout.write(
            "\nNota: sin secciones previas, que en la corrida real ocupan "
            "presupuesto. Estos números son el techo, no la corrida."
        )

    def _report_cache(self, cached: list[int], uncached: list[int]) -> None:
        """Qué se paga una vez y qué se paga en cada paso.

        Es la comprobación de que el punto de caché quedó donde tiene que
        quedar. Si la parte estable no es idéntica en todos los pasos, no hay
        caché que valga: el prefijo se rompe en el primer byte distinto y el
        corpus se cobra entero de nuevo. Por eso se compara y se avisa, en vez
        de asumirlo.
        """
        if not cached:
            return
        self.stdout.write("")
        distinct = set(cached)
        if len(distinct) > 1:
            self.stdout.write(
                self.style.ERROR(
                    "La parte cacheable NO es idéntica entre pasos "
                    f"({len(distinct)} tamaños distintos): la caché se invalida "
                    "y el corpus se paga una vez por paso."
                )
            )
            return

        steps = len(cached)
        stable = cached[0]
        variable = sum(uncached)
        # Escritura de caché 1,25x, lectura 0,1x.
        with_cache = stable * 1.25 + stable * 0.1 * (steps - 1) + variable
        without_cache = stable * steps + variable
        self.stdout.write(
            self.style.SUCCESS(
                f"Parte cacheable idéntica en los {steps} pasos: {stable:,} tokens."
            )
        )
        self.stdout.write(
            f"  entrada facturable con caché:  {with_cache:>12,.0f} tokens\n"
            f"  entrada facturable sin caché:  {without_cache:>12,.0f} tokens\n"
            f"  ahorro:                        "
            f"{(1 - with_cache / without_cache) * 100:>11.0f}%"
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
        # Los workflows provistos por Ecofilia no tienen dueño (`owner=None`
        # significa plantilla visible para todos), así que el dueño sale de la
        # operación. Sin esto la recuperación dentro de los documentos
        # degradados falla y el preview mide una parte variable que no existe.
        owner = skill.owner or project.owner
        if owner is None:
            raise CommandError(
                "Ni el workflow ni la operación tienen dueño; la recuperación "
                "necesita un usuario para resolver permisos."
            )
        # Sin `save()`: el comando no deja rastro en el historial de corridas.
        return SkillExecution(skill=skill, owner=owner, project=project, metadata={})
