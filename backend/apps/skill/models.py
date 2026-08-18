from __future__ import annotations

import os
from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

DEFAULT_MODEL = os.environ.get("MODEL_COMPLETION", "gpt-4o-mini")


class SkillType(models.TextChoices):
    QUICK = "quick", _("Quick")
    COPILOT = "copilot", _("Copilot")


class RetrievalStrategy(models.TextChoices):
    GLOBAL = "global", _("Global")
    HYBRID_PER_DOCUMENT = "hybrid_per_document", _("Hybrid Per Document")


class SkillContext(models.TextChoices):
    REPOSITORY = "repository", _("Repository")
    PROJECT = "project", _("Project")
    DOCUMENT = "document", _("Document")
    ANY = "any", _("Any")


class SkillTier(models.TextChoices):
    """
    Capacidad pedida, no un modelo concreto.

    Guardar un id de modelo en la base envejece mal: el workflow del IET quedó
    con `gpt-4o-mini` congelado desde su migración y nadie se enteró hasta que
    se auditó por qué las corridas variaban. El tier evita repetirlo — el autor
    elige cuánta capacidad necesita el paso y el modelo concreto lo resuelve
    `LLM_MODEL_FAST` / `_BALANCED` / `_DEEP` en tiempo de request, así que una
    generación nueva de modelos se adopta cambiando una variable de entorno.
    """
    FAST = "fast", _("Rápido")
    BALANCED = "balanced", _("Equilibrado")
    DEEP = "deep", _("Profundo")


class ExecutionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    AWAITING_APPROVAL = "awaiting_approval", _("Awaiting Approval")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")


class ExecutionOutputMode(models.TextChoices):
    TEXT = "text", _("Text")
    TABLE = "table", _("Table")


# ---------------------------------------------------------------------------
# Skill definition
# ---------------------------------------------------------------------------

class Skill(models.Model):
    """
    A reusable AI automation template.

    owner=None means it's an Ecofilia-provided template visible to all users.
    """
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="skills",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    skill_type = models.CharField(
        max_length=20, choices=SkillType.choices, default=SkillType.QUICK
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, blank=True, max_length=255)
    description = models.TextField(blank=True)

    # Contexts where this skill can be executed
    allowed_contexts = models.JSONField(
        default=list,
        help_text='List of allowed contexts, e.g. ["repository", "project", "document"]',
    )

    # LLM config
    system_prompt = models.TextField(
        default=(
            "Eres Ecofilia, un asistente especializado en sostenibilidad. "
            "Responde siempre basándote en los documentos provistos y cita las fuentes."
        )
    )
    # For QUICK: full prompt template. Use {{context}} for doc content,
    # {{extra_instructions}} for user overrides.
    prompt_template = models.TextField(
        blank=True,
        help_text=(
            "For QUICK skills: use {{context}} to inject document content "
            "and {{extra_instructions}} for optional user instructions."
        ),
    )
    tier = models.CharField(
        max_length=20,
        choices=SkillTier.choices,
        default=SkillTier.BALANCED,
        help_text=(
            "Capacidad por defecto de la skill. Cada paso puede pedir otra. "
            "El equilibrado alcanza para redacción muy instruida sobre "
            "evidencia acotada; el profundo es para síntesis y juicio."
        ),
    )
    # Precede al tier sólo si guarda un id de Claude explícito: es la escotilla
    # de escape para fijar un modelo puntual, no el control primario.
    model = models.CharField(max_length=100, default=DEFAULT_MODEL)
    temperature = models.FloatField(default=0.3)
    comparative_mode_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When enabled, enforce per-document comparative output and use "
            "hybrid retrieval strategy by default."
        ),
    )
    strict_missing_evidence = models.BooleanField(
        default=True,
        help_text=(
            "When comparative mode is enabled, require explicit 'no evidence' "
            "statements for missing document/criterion pairs."
        ),
    )
    retrieval_query_template = models.TextField(
        blank=True,
        help_text=(
            "Optional explicit retrieval query. When set, used instead of the "
            "auto-built query from name+description. Supports {{extra_instructions}} placeholder."
        ),
    )
    retrieval_strategy = models.CharField(
        max_length=40,
        choices=RetrievalStrategy.choices,
        default=RetrievalStrategy.GLOBAL,
        help_text="Chunk retrieval strategy to build model context.",
    )
    k_per_doc = models.PositiveSmallIntegerField(
        default=2,
        help_text="Candidate chunks to retrieve per document in hybrid mode.",
    )
    total_limit = models.PositiveSmallIntegerField(
        default=12,
        help_text="Maximum number of chunks included in the final merged context.",
    )
    max_per_doc_after_rerank = models.PositiveSmallIntegerField(
        default=4,
        help_text="Max chunks kept per document after global reranking.",
    )

    default_output_mode = models.CharField(
        max_length=20,
        choices=ExecutionOutputMode.choices,
        default=ExecutionOutputMode.TEXT,
        help_text=(
            "Default output mode for this skill. When set to 'table', the skill produces "
            "structured tabular output using the configured table_schema."
        ),
    )
    table_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Persistent schema for tabular output. Expected shape: "
            '{"name": str, "description": str, "columns": [TableColumn]}.'
        ),
    )
    pinned_document_slugs = models.JSONField(
        default=list,
        blank=True,
        help_text="Default document filter. Empty = all context documents.",
    )

    # ---------------------------------------------------------------------------
    # Agentic capabilities (Sprint 1 + 2)
    # ---------------------------------------------------------------------------

    tools_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When enabled, the runner uses function-calling so the model can "
            "invoke tools (search_more_context, calculate_ghg_emissions, etc.) "
            "before producing its final answer."
        ),
    )

    # Research phase: runs broad retrieval before authoring steps (Copilot only).
    research_phase_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Copilot only. When enabled, the runner executes a research phase that "
            "builds a shared scratchpad from the full document corpus before running "
            "authoring steps. Each step receives the scratchpad as additional context."
        ),
    )
    research_queries = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Optional list of explicit queries for the research phase. "
            "If empty, queries are auto-derived from step titles and instructions."
        ),
    )

    # True = Ecofilia-provided, non-editable by regular users
    is_template = models.BooleanField(default=False)
    is_default_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When true, this skill is enabled by default in every repository/project "
            "workspace unless the user adds more plugins."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("skill_type", "name")
        indexes = [models.Index(fields=("owner", "skill_type"))]

    def __str__(self) -> str:
        return f"{self.name} ({self.skill_type})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._unique_slug()
        super().save(*args, **kwargs)

    def _unique_slug(self) -> str:
        base = slugify(self.name) or "skill"
        slug, counter = base[:255], 1
        while Skill.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            suffix = f"-{counter}"
            slug = f"{base[:255 - len(suffix)]}{suffix}"
            counter += 1
        return slug

    def can_edit(self, user) -> bool:
        if user.is_staff:
            return True
        if self.is_template:
            return False
        return self.owner_id == user.id


# ---------------------------------------------------------------------------
# Typed input parameters  (Sprint 2 — replaces the free-text extra_instructions blob)
# ---------------------------------------------------------------------------

class SkillParameterType(models.TextChoices):
    TEXT = "text", _("Text")
    NUMBER = "number", _("Number")
    ENUM = "enum", _("Enum (select)")
    BOOLEAN = "boolean", _("Boolean")
    DATE = "date", _("Date")


class SkillParameter(models.Model):
    """
    A typed input parameter declared on a Skill.

    Templates reference parameters with {{key}} — e.g. {{framework}}, {{target_year}}.
    When the skill is run, the caller supplies values in SkillExecution.input_values.
    """
    skill = models.ForeignKey(Skill, related_name="parameters", on_delete=models.CASCADE)
    key = models.SlugField(
        max_length=80,
        help_text="Template variable name, e.g. 'framework' → {{framework}}.",
    )
    label = models.CharField(max_length=255, help_text="Human-readable label shown in the UI.")
    param_type = models.CharField(
        max_length=20,
        choices=SkillParameterType.choices,
        default=SkillParameterType.TEXT,
    )
    description = models.TextField(
        blank=True,
        help_text="Help text shown beneath the input field.",
    )
    default_value = models.CharField(
        max_length=500,
        blank=True,
        help_text="String representation of the default value.",
    )
    required = models.BooleanField(default=False)
    options = models.JSONField(
        default=list,
        blank=True,
        help_text="Allowed values for enum type. e.g. ['GRI', 'ISSB', 'CDP'].",
    )
    position = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ("position",)
        unique_together = ("skill", "key")

    def __str__(self) -> str:
        return f"{self.skill.name} — {{{{ {self.key} }}}} ({self.param_type})"


# ---------------------------------------------------------------------------
# Copilot steps  (ordered sections for COPILOT skills)
# ---------------------------------------------------------------------------

class SkillStepType(models.TextChoices):
    INSTRUCTION = "instruction", _("Instruction")
    SKILL_REF = "skill_ref", _("Run existing skill")


class OutputValidation(models.TextChoices):
    """Qué hace el motor cuando la salida no cumple el contrato del paso.

    Es por paso y no del motor a propósito. Una determinación auditable y una
    exploración no piden lo mismo: en la primera, una celda fuera del vocabulario
    declarado es un error que hay que ver; en la segunda, exigir el contrato
    convierte cada corrida en una pelea contra el validador.

    Distintos workflows —y distintos pasos del mismo— van a querer cosas
    distintas, así que la rigidez la decide quien escribe el workflow.
    """

    LENIENT = "lenient", "Tolerante: normaliza lo que puede y registra el resto"
    STRICT = "strict", "Estricto: la salida cumple el contrato o el paso falla"


class StepEvidenceMode(models.TextChoices):
    """
    De dónde saca su material un paso.

    No todos los pasos de un informe leen documentos. Los que integran —"indicá
    la determinación a partir de los tres criterios anteriores"— trabajan sobre
    lo ya redactado, y darles evidencia documental además de sobrar los expone a
    citar documentos que no corresponden a esa sección.
    """
    DOCUMENTS = "documents", _("Solo documentos")
    PREVIOUS = "previous_steps", _("Solo pasos previos")
    BOTH = "both", _("Documentos y pasos previos")


class SkillStep(models.Model):
    skill = models.ForeignKey(Skill, related_name="steps", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    instructions = models.TextField(
        blank=True,
        help_text="What the AI should produce for this section of the output.",
    )
    position = models.PositiveIntegerField(default=1)

    # Step type — an "instruction" step authors content from the step's own
    # prompt; a "skill_ref" step runs an existing QUICK skill inline and folds
    # its output into the workflow as this step's content.
    step_type = models.CharField(
        max_length=20,
        choices=SkillStepType.choices,
        default=SkillStepType.INSTRUCTION,
        help_text=(
            "'instruction' authors from the step's own prompt. 'skill_ref' runs "
            "an existing quick skill inline and uses its output as this step."
        ),
    )
    linked_skill = models.ForeignKey(
        Skill,
        related_name="referenced_in_steps",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=(
            "Only for step_type='skill_ref': the quick skill to execute for this "
            "step. Its prompt/retrieval config is used against this step's documents."
        ),
    )
    # Per-step document scope. When non-empty, this step only sees these
    # documents (intersected with the execution context). Empty = all context
    # documents, preserving the original behaviour.
    document_slugs = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Optional subset of document slugs this step runs against. "
            "Empty = all documents in the execution context."
        ),
    )
    tier = models.CharField(
        max_length=20,
        choices=SkillTier.choices,
        blank=True,
        default="",
        help_text=(
            "Capacidad para este paso. Vacío = la del workflow. Los pasos de un "
            "mismo informe no son homogéneos: describir un marco de políticas a "
            "partir de documentos no pide lo mismo que integrar criterios en una "
            "determinación."
        ),
    )
    evidence_mode = models.CharField(
        max_length=20,
        choices=StepEvidenceMode.choices,
        default=StepEvidenceMode.BOTH,
        help_text=(
            "Con qué material trabaja el paso. Los pasos que integran resultados "
            "anteriores no deberían recibir documentos: no los necesitan y es una "
            "vía por la que terminan citando fuentes ajenas a su sección."
        ),
    )
    output_mode = models.CharField(
        max_length=20,
        choices=ExecutionOutputMode.choices,
        default=ExecutionOutputMode.TEXT,
        help_text=(
            "Output mode for this step. When set to 'table', the step must define a "
            "table_schema so the runner produces structured rows."
        ),
    )
    approval_required = models.BooleanField(
        default=False,
        help_text=(
            "When enabled, the copilot pauses after this step completes and waits for "
            "the consultant to review, optionally edit, and approve before continuing."
        ),
    )
    table_schema = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Persistent schema for tabular output of this step. Expected shape: "
            '{"name": str, "description": str, "columns": [TableColumn]}.'
        ),
    )
    output_validation = models.CharField(
        max_length=20,
        choices=OutputValidation.choices,
        default=OutputValidation.STRICT,
        help_text=(
            "Qué hacer cuando la salida no cumple el esquema declarado. "
            "'strict' hace fallar el paso; 'lenient' normaliza lo que puede y "
            "registra el resto. Los pasos que ya existían quedaron en 'lenient' "
            "para no cambiarles el comportamiento; los nuevos nacen estrictos."
        ),
    )

    class Meta:
        ordering = ("position",)
        unique_together = ("skill", "position")

    def __str__(self) -> str:
        return f"{self.skill.name} — Step {self.position}: {self.title}"


# ---------------------------------------------------------------------------
# Definition versions
# ---------------------------------------------------------------------------

class SkillDefinitionVersion(models.Model):
    """
    Una definición de workflow congelada, tal como se ejecutó.

    La definición vive editable en la base: eso es deliberado —el template y las
    instrucciones tienen que poder mutar— pero deja las corridas sin suelo. Se
    corrige la instrucción de un paso, se vuelve a correr, sale distinto, y no
    hay forma de saber si cambió el modelo o cambió el pedido.

    Cada corrida queda apuntando a la fila que corresponde a la definición con la
    que arrancó. La versión no se publica a mano: se deriva de la definición
    misma (ver ``definition.resolve_definition_version``), así que no depende de
    que alguien se acuerde. La identidad es la huella, no el número — dos
    definiciones idénticas son la misma versión aunque se haya editado y
    revertido en el medio, porque para comparar corridas eso es justamente lo
    que importa.
    """

    skill = models.ForeignKey(
        Skill, related_name="definition_versions", on_delete=models.CASCADE
    )
    version_number = models.PositiveIntegerField()
    fingerprint = models.CharField(
        max_length=80,
        help_text="sha256 de la definición serializada. Es su identidad real.",
    )
    schema = models.PositiveSmallIntegerField(
        default=1,
        help_text=(
            "Versión del formato de serialización. Cambiarlo mueve todas las "
            "huellas, así que sin este campo un cambio nuestro sería "
            "indistinguible de un cambio del autor del workflow."
        ),
    )
    definition = models.JSONField(
        help_text="Snapshot completo: skill, pasos y parámetros declarados.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("skill", "-version_number")
        constraints = [
            models.UniqueConstraint(
                fields=("skill", "version_number"), name="skill_definition_version_number"
            ),
            models.UniqueConstraint(
                fields=("skill", "fingerprint"), name="skill_definition_version_fingerprint"
            ),
        ]
        indexes = [models.Index(fields=("skill", "fingerprint"))]

    def __str__(self) -> str:
        return f"{self.skill.slug} def v{self.version_number}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class SkillExecution(models.Model):
    """
    A single run of a Skill against a specific context.
    """
    skill = models.ForeignKey(Skill, related_name="executions", on_delete=models.CASCADE)
    definition_version = models.ForeignKey(
        SkillDefinitionVersion,
        related_name="executions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text=(
            "La definición con la que arrancó esta corrida. Vacío en las "
            "corridas anteriores al versionado: no se rellenan con una versión "
            "sintética porque afirmaría que corrieron la definición de hoy, que "
            "es exactamente la mentira que el versionado viene a evitar."
        ),
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="skill_executions",
        on_delete=models.CASCADE,
    )

    # Context — exactly one should be set (or none for ANY context testing)
    repository = models.ForeignKey(
        "repository.Repository",
        related_name="skill_executions",
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    project = models.ForeignKey(
        "project.Project",
        related_name="skill_executions",
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    document = models.ForeignKey(
        "document.Document",
        related_name="skill_executions",
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    # Optional user-provided instructions that override / extend the skill
    extra_instructions = models.TextField(blank=True)

    # Typed parameter values supplied at run time, keyed by SkillParameter.key.
    input_values = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Values for the skill's declared SkillParameter inputs, keyed by parameter key. "
            "Rendered into the prompt via {{key}} template tokens."
        ),
    )
    output_mode = models.CharField(
        max_length=20,
        choices=ExecutionOutputMode.choices,
        default=ExecutionOutputMode.TEXT,
        help_text="Requested output shape for this execution (text or table).",
    )

    # Execution state
    status = models.CharField(
        max_length=20, choices=ExecutionStatus.choices, default=ExecutionStatus.PENDING
    )

    # QUICK output: plain text (markdown)
    output = models.TextField(blank=True)

    # COPILOT output: {"steps": [{"step_id": 1, "title": "...", "content": "..."}]}
    output_structured = models.JSONField(default=dict, blank=True)

    # User-curated edit of the execution output. When non-empty this becomes
    # the "current" content shown in the result view; the raw AI output stays
    # intact under `output` / `output_structured` so the user can always
    # diff or revert to the original.
    edited_output = models.TextField(
        blank=True,
        help_text=(
            "Latest user-edited markdown of this execution's result. Empty means "
            "the user has not modified the AI-generated output yet."
        ),
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="edited_skill_executions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    # How many copilot steps have been written so far (updated incrementally during execution).
    steps_completed = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of copilot steps that have been written to output_structured so far.",
    )
    # Sprint 4 — human-in-the-loop
    current_step_position = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "When status=awaiting_approval, holds the position of the step currently "
            "awaiting review. Null when the execution is not paused."
        ),
    )

    # Snapshot of which documents were used (for reproducibility)
    document_snapshot = models.JSONField(default=list)

    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)  # token usage, timing, etc.

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("owner", "created_at")),
            models.Index(fields=("skill", "status")),
        ]

    def __str__(self) -> str:
        return f"Execution {self.id} — {self.skill.name} ({self.status})"

    @property
    def steps_total(self) -> int:
        """Total number of steps defined for this copilot execution."""
        if self.skill.skill_type == "copilot":
            return self.skill.steps.count()
        return 0

    @property
    def context_label(self) -> str:
        if self.repository_id:
            return f"Repository: {self.repository.name}"
        if self.project_id:
            return f"Project: {self.project.name}"
        if self.document_id:
            return f"Document: {self.document.name}"
        return "No context"


# ---------------------------------------------------------------------------
# Execution version history
# ---------------------------------------------------------------------------

class SkillExecutionVersion(models.Model):
    """
    Immutable snapshot of a SkillExecution's edited output.

    Each save of `edited_output` produces a new version row so the user can
    review or restore previous iterations. The first version is created the
    first time the user saves an edit; the raw AI output (pre-edit) is
    intentionally NOT versioned here — it lives on the execution itself.
    """
    execution = models.ForeignKey(
        SkillExecution,
        related_name="versions",
        on_delete=models.CASCADE,
    )
    version_number = models.PositiveIntegerField()
    label = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional short note the user attaches to this save point.",
    )
    content = models.TextField(
        help_text="Markdown snapshot of edited_output at the time of save.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="skill_execution_versions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-version_number",)
        unique_together = ("execution", "version_number")
        indexes = [
            models.Index(fields=("execution", "version_number")),
        ]

    def __str__(self) -> str:
        return f"Execution {self.execution_id} v{self.version_number}"
