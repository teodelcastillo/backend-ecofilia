from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.document.services import accessible_documents_for
from apps.skill.models import (
    ExecutionOutputMode,
    OutputValidation,
    ExecutionStatus,
    RetrievalStrategy,
    Skill,
    SkillContext,
    SkillDefinitionVersion,
    SkillExecution,
    SkillExecutionVersion,
    SkillParameter,
    SkillParameterType,
    SkillStep,
    SkillStepType,
    SkillTier,
    StepEvidenceMode,
    SkillType,
)
from apps.skill.table_schema import (
    TableSchemaError,
    normalize_table_schema,
    schema_has_columns,
)

User = get_user_model()

ALLOWED_CONTEXT_VALUES = {c.value for c in SkillContext}
MULTI_DOCUMENT_CONTEXTS = {SkillContext.REPOSITORY.value, SkillContext.PROJECT.value}
DOCUMENT_FIRST_KEYWORDS = (
    "compar",
    "versus",
    "vs ",
    "benchmark",
    "checklist",
    "extract",
    "snapshot",
    "map",
    "diagnosis",
    "table",
    "matriz",
    "criter",
)


def _requires_document_first_analysis(
    *,
    skill_type: str,
    allowed_contexts: list[str],
    name: str,
    description: str,
    prompt_template: str,
) -> bool:
    has_multi_document_scope = bool(set(allowed_contexts or []).intersection(MULTI_DOCUMENT_CONTEXTS))
    if not has_multi_document_scope:
        return False
    if skill_type == SkillType.COPILOT:
        return True
    text = f"{name} {description} {prompt_template}".lower()
    return any(keyword in text for keyword in DOCUMENT_FIRST_KEYWORDS)


def _normalize_table_schema_or_raise(raw, *, field: str) -> dict:
    try:
        return normalize_table_schema(raw)
    except TableSchemaError as exc:
        raise serializers.ValidationError({field: str(exc)})


# ---------------------------------------------------------------------------
# Skill parameters (Sprint 2B)
# ---------------------------------------------------------------------------

class SkillParameterSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkillParameter
        fields = (
            "id",
            "key",
            "label",
            "param_type",
            "description",
            "default_value",
            "required",
            "options",
            "position",
        )


class SkillParameterWriteSerializer(serializers.Serializer):
    key = serializers.SlugField(max_length=80)
    label = serializers.CharField(max_length=255)
    param_type = serializers.ChoiceField(choices=SkillParameterType.choices, default=SkillParameterType.TEXT)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    default_value = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)
    required = serializers.BooleanField(required=False, default=False)
    options = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
    )
    position = serializers.IntegerField(min_value=1, default=1)

    def validate(self, attrs):
        if attrs.get("param_type") == SkillParameterType.ENUM and not attrs.get("options"):
            raise serializers.ValidationError(
                {"options": "Enum parameters require at least one option."}
            )
        return attrs


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class SkillStepSerializer(serializers.ModelSerializer):
    linked_skill_slug = serializers.SlugField(
        source="linked_skill.slug", read_only=True, allow_null=True,
    )
    linked_skill_name = serializers.CharField(
        source="linked_skill.name", read_only=True, allow_null=True,
    )

    class Meta:
        model = SkillStep
        fields = (
            "id",
            "title",
            "instructions",
            "position",
            "step_type",
            "tier",
            "evidence_mode",
            "linked_skill_slug",
            "linked_skill_name",
            "document_slugs",
            "output_mode",
            "table_schema",
            "output_validation",
            "approval_required",
        )


class SkillSerializer(serializers.ModelSerializer):
    owner_email = serializers.EmailField(source="owner.email", read_only=True, allow_null=True)
    steps = SkillStepSerializer(many=True, read_only=True)
    parameters = SkillParameterSerializer(many=True, read_only=True)

    class Meta:
        model = Skill
        fields = (
            "id", "slug", "name", "description", "skill_type",
            "allowed_contexts", "system_prompt", "prompt_template",
            "tier", "model", "temperature",
            "comparative_mode_enabled", "strict_missing_evidence",
            "retrieval_query_template",
            "retrieval_strategy", "k_per_doc", "total_limit", "max_per_doc_after_rerank",
            "default_output_mode", "table_schema",
            "pinned_document_slugs",
            # Sprint 1 + 2
            "tools_enabled",
            "research_phase_enabled", "research_queries",
            "parameters",
            "is_template", "is_default_enabled",
            "owner", "owner_email", "steps",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "slug", "is_template", "is_default_enabled",
            "owner", "owner_email", "created_at", "updated_at",
        )


class SkillStepWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    # Instructions are required for instruction steps but optional for
    # skill_ref steps (the linked skill carries its own prompt). Validation
    # below enforces the per-type rule.
    instructions = serializers.CharField(required=False, allow_blank=True, default="")
    position = serializers.IntegerField(min_value=1)
    step_type = serializers.ChoiceField(
        choices=SkillStepType.choices,
        required=False,
        default=SkillStepType.INSTRUCTION,
    )
    tier = serializers.ChoiceField(
        choices=SkillTier.choices,
        required=False,
        allow_blank=True,
        default="",
        help_text="Vacío hereda el tier del workflow.",
    )
    evidence_mode = serializers.ChoiceField(
        choices=StepEvidenceMode.choices,
        required=False,
        default=StepEvidenceMode.BOTH,
        help_text="Con qué material trabaja el paso: documentos, pasos previos, o ambos.",
    )
    linked_skill_slug = serializers.SlugField(
        required=False, allow_null=True, allow_blank=True, default=None,
    )
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        required=False,
        allow_empty=True,
        default=list,
    )
    output_mode = serializers.ChoiceField(
        choices=ExecutionOutputMode.choices,
        required=False,
        default=ExecutionOutputMode.TEXT,
    )
    output_validation = serializers.ChoiceField(
        choices=OutputValidation.choices,
        required=False,
        default=OutputValidation.STRICT,
        help_text=(
            "Qué hacer cuando la salida no cumple el esquema declarado. Un paso "
            "nuevo nace estricto: el contrato se cumple o el paso falla. Los pasos "
            "que ya existían quedaron en 'lenient' para no cambiarles el "
            "comportamiento."
        ),
    )
    table_schema = serializers.DictField(required=False)
    approval_required = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        step_type = attrs.get("step_type", SkillStepType.INSTRUCTION)
        linked_slug = attrs.get("linked_skill_slug")
        instructions = (attrs.get("instructions") or "").strip()

        if step_type == SkillStepType.SKILL_REF:
            # A skill_ref step must reference an existing QUICK skill the user
            # can access. Validation of the actual object happens in the parent
            # serializer's create/update where we have the request user.
            if not linked_slug:
                raise serializers.ValidationError(
                    {"linked_skill_slug": "Skill-reference steps require a linked skill."}
                )
            # skill_ref steps inherit the linked skill's output shape; force text.
            attrs["output_mode"] = ExecutionOutputMode.TEXT
            attrs["table_schema"] = {}
            return attrs

        # instruction step
        attrs["linked_skill_slug"] = None
        if not instructions:
            raise serializers.ValidationError(
                {"instructions": "Instruction steps require instructions."}
            )

        output_mode = attrs.get("output_mode", ExecutionOutputMode.TEXT)
        table_schema = attrs.get("table_schema") or {}
        if output_mode == ExecutionOutputMode.TABLE:
            attrs["table_schema"] = _normalize_table_schema_or_raise(
                table_schema, field="table_schema"
            )
        else:
            if schema_has_columns(table_schema):
                raise serializers.ValidationError(
                    {"table_schema": "table_schema is only allowed when output_mode='table'."}
                )
            attrs["table_schema"] = {}
        return attrs


class SkillWriteSerializer(serializers.ModelSerializer):
    steps = SkillStepWriteSerializer(many=True, required=False)
    parameters = SkillParameterWriteSerializer(many=True, required=False)
    table_schema = serializers.DictField(required=False)
    default_output_mode = serializers.ChoiceField(
        choices=ExecutionOutputMode.choices,
        required=False,
        default=ExecutionOutputMode.TEXT,
    )

    class Meta:
        model = Skill
        fields = (
            "name", "description", "skill_type",
            "allowed_contexts", "system_prompt", "prompt_template",
            "tier", "model", "temperature",
            "comparative_mode_enabled", "strict_missing_evidence",
            "retrieval_query_template",
            "retrieval_strategy", "k_per_doc", "total_limit", "max_per_doc_after_rerank",
            "default_output_mode", "table_schema",
            "pinned_document_slugs",
            # Sprint 1 + 2
            "tools_enabled",
            "research_phase_enabled", "research_queries",
            "steps",
            "parameters",
        )

    def validate_allowed_contexts(self, value):
        if not value:
            raise serializers.ValidationError("At least one allowed context is required.")
        invalid = set(value) - ALLOWED_CONTEXT_VALUES
        if invalid:
            raise serializers.ValidationError(
                f"Invalid values: {sorted(invalid)}. "
                f"Allowed: {sorted(ALLOWED_CONTEXT_VALUES)}."
            )
        return value

    def validate(self, attrs):
        existing = self.instance
        skill_type = attrs.get(
            "skill_type",
            existing.skill_type if existing else SkillType.QUICK,
        )
        steps = attrs.get("steps", [])
        allowed_contexts = attrs.get(
            "allowed_contexts",
            existing.allowed_contexts if existing else [],
        )
        comparative_mode_enabled = attrs.get(
            "comparative_mode_enabled",
            existing.comparative_mode_enabled if existing else False,
        )
        retrieval_strategy = attrs.get(
            "retrieval_strategy",
            existing.retrieval_strategy if existing else RetrievalStrategy.GLOBAL,
        )
        k_per_doc = attrs.get("k_per_doc", existing.k_per_doc if existing else 2)
        total_limit = attrs.get("total_limit", existing.total_limit if existing else 12)
        max_per_doc_after_rerank = attrs.get(
            "max_per_doc_after_rerank",
            existing.max_per_doc_after_rerank if existing else 4,
        )

        if skill_type == SkillType.COPILOT and not steps:
            raise serializers.ValidationError(
                {"steps": "Copilot skills require at least one step."}
            )
        effective_prompt_template = attrs.get(
            "prompt_template",
            existing.prompt_template if existing else "",
        )
        name = attrs.get("name", existing.name if existing else "")
        description = attrs.get("description", existing.description if existing else "")

        if _requires_document_first_analysis(
            skill_type=skill_type,
            allowed_contexts=allowed_contexts,
            name=name,
            description=description,
            prompt_template=effective_prompt_template,
        ):
            comparative_mode_enabled = True
            attrs["comparative_mode_enabled"] = True
            attrs["retrieval_strategy"] = RetrievalStrategy.HYBRID_PER_DOCUMENT
        if skill_type == SkillType.QUICK and not effective_prompt_template.strip():
            raise serializers.ValidationError(
                {"prompt_template": "Quick skills require a prompt template."}
            )
        if retrieval_strategy not in {choice for choice, _ in RetrievalStrategy.choices}:
            raise serializers.ValidationError(
                {"retrieval_strategy": "Invalid retrieval strategy."}
            )
        if k_per_doc < 1 or k_per_doc > 10:
            raise serializers.ValidationError(
                {"k_per_doc": "k_per_doc must be between 1 and 10."}
            )
        if total_limit < 1 or total_limit > 50:
            raise serializers.ValidationError(
                {"total_limit": "total_limit must be between 1 and 50."}
            )
        if max_per_doc_after_rerank < 1 or max_per_doc_after_rerank > 20:
            raise serializers.ValidationError(
                {
                    "max_per_doc_after_rerank": (
                        "max_per_doc_after_rerank must be between 1 and 20."
                    )
                }
            )
        if max_per_doc_after_rerank > total_limit:
            raise serializers.ValidationError(
                {
                    "max_per_doc_after_rerank": (
                        "max_per_doc_after_rerank cannot exceed total_limit."
                    )
                }
            )
        if comparative_mode_enabled and retrieval_strategy == RetrievalStrategy.GLOBAL:
            attrs["retrieval_strategy"] = RetrievalStrategy.HYBRID_PER_DOCUMENT

        # Validate skill-level table schema (only relevant for QUICK skills)
        default_output_mode = attrs.get(
            "default_output_mode",
            existing.default_output_mode if existing else ExecutionOutputMode.TEXT,
        )
        table_schema = attrs.get(
            "table_schema",
            existing.table_schema if existing else {},
        )
        if skill_type == SkillType.QUICK and default_output_mode == ExecutionOutputMode.TABLE:
            attrs["table_schema"] = _normalize_table_schema_or_raise(
                table_schema, field="table_schema"
            )
        elif skill_type == SkillType.COPILOT:
            if default_output_mode == ExecutionOutputMode.TABLE:
                raise serializers.ValidationError(
                    {
                        "default_output_mode": (
                            "Copilot skills cannot set default_output_mode='table'. "
                            "Configure each step's output_mode instead."
                        )
                    }
                )
            attrs["table_schema"] = {}
        else:
            if schema_has_columns(table_schema):
                raise serializers.ValidationError(
                    {
                        "table_schema": (
                            "table_schema is only allowed when default_output_mode='table'."
                        )
                    }
                )
            attrs["table_schema"] = {}

        return attrs

    def _resolve_linked_skill(self, slug: str):
        """
        Resolve a linked_skill_slug to a QUICK Skill the user can reference.
        Only quick skills can be used as workflow sub-steps; copilot skills
        would recurse. Templates and the user's own skills are allowed.
        """
        if not slug:
            return None
        request = self.context.get("request")
        user = request.user if request else None
        from django.db.models import Q

        qs = Skill.objects.filter(slug=slug, skill_type=SkillType.QUICK)
        if user is not None and not user.is_staff:
            qs = qs.filter(Q(owner__isnull=True) | Q(owner=user))
        skill = qs.first()
        if skill is None:
            raise serializers.ValidationError(
                {
                    "steps": (
                        f"La skill referenciada '{slug}' no existe, no es una quick "
                        "skill, o no tenés acceso a ella."
                    )
                }
            )
        return skill

    def _materialize_step(self, skill: Skill, step_data: dict) -> SkillStep:
        data = dict(step_data)
        linked_slug = data.pop("linked_skill_slug", None)
        step_type = data.get("step_type", SkillStepType.INSTRUCTION)
        linked_skill = (
            self._resolve_linked_skill(linked_slug)
            if step_type == SkillStepType.SKILL_REF
            else None
        )
        return SkillStep.objects.create(
            skill=skill, linked_skill=linked_skill, **data
        )

    def create(self, validated_data):
        steps_data = validated_data.pop("steps", [])
        parameters_data = validated_data.pop("parameters", [])
        skill = Skill.objects.create(**validated_data)
        for step in steps_data:
            self._materialize_step(skill, step)
        for param in parameters_data:
            SkillParameter.objects.create(skill=skill, **param)
        return skill

    def update(self, instance, validated_data):
        steps_data = validated_data.pop("steps", None)
        parameters_data = validated_data.pop("parameters", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if steps_data is not None:
            instance.steps.all().delete()
            for step in steps_data:
                self._materialize_step(instance, step)
        if parameters_data is not None:
            instance.parameters.all().delete()
            for param in parameters_data:
                SkillParameter.objects.create(skill=instance, **param)
        return instance


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class SkillExecutionSerializer(serializers.ModelSerializer):
    skill_name = serializers.CharField(source="skill.name", read_only=True)
    skill_type = serializers.CharField(source="skill.skill_type", read_only=True)
    context_label = serializers.CharField(read_only=True)
    # Resolve context slugs for the frontend
    repository_slug = serializers.SlugField(source="repository.slug", read_only=True, allow_null=True)
    project_slug = serializers.SlugField(source="project.slug", read_only=True, allow_null=True)
    document_slug = serializers.SlugField(source="document.slug", read_only=True, allow_null=True)
    # Sprint 3 — incremental progress
    steps_total = serializers.IntegerField(read_only=True)
    # Editable output state
    edited_by_email = serializers.EmailField(source="edited_by.email", read_only=True, allow_null=True)
    versions_count = serializers.SerializerMethodField()
    # Qué definición corrió. Sin esto el frente puede mostrar la salida pero no
    # con qué instrucciones se produjo, que es la mitad de la trazabilidad.
    definition_version_number = serializers.IntegerField(
        source="definition_version.version_number", read_only=True, allow_null=True,
    )
    definition_fingerprint = serializers.CharField(
        source="definition_version.fingerprint", read_only=True, allow_null=True,
    )

    class Meta:
        model = SkillExecution
        fields = (
            "id", "skill", "skill_name", "skill_type",
            "status", "context_label",
            "repository_slug", "project_slug", "document_slug",
            "extra_instructions", "input_values", "output_mode",
            "output", "output_structured",
            "edited_output", "edited_at", "edited_by_email", "versions_count",
            "steps_completed", "steps_total", "current_step_position",
            "document_snapshot", "metadata", "error_message",
            "definition_version", "definition_version_number", "definition_fingerprint",
            "started_at", "finished_at", "created_at",
        )
        read_only_fields = fields

    def get_versions_count(self, obj) -> int:
        return obj.versions.count() if obj.pk else 0


class SkillDefinitionVersionSerializer(serializers.ModelSerializer):
    """Una versión de la definición, para poder leer con qué se corrió."""

    skill_slug = serializers.SlugField(source="skill.slug", read_only=True)
    executions_count = serializers.SerializerMethodField()

    class Meta:
        model = SkillDefinitionVersion
        fields = (
            "id", "skill_slug", "version_number", "fingerprint",
            "schema", "definition", "executions_count", "created_at",
        )
        read_only_fields = fields

    def get_executions_count(self, obj) -> int:
        return obj.executions.count()


class RerunExecutionSerializer(serializers.Serializer):
    """Input para POST /api/skill-executions/{id}/rerun/"""

    # Por defecto se hereda de la corrida original. Para una comparación de
    # reproducibilidad conviene apagarlo: nadie quiere aprobar diecisiete pasos
    # a mano para medir si el motor repite.
    review_each_step = serializers.BooleanField(required=False, allow_null=True, default=None)


class SkillExecutionVersionSerializer(serializers.ModelSerializer):
    created_by_email = serializers.EmailField(
        source="created_by.email", read_only=True, allow_null=True,
    )

    class Meta:
        model = SkillExecutionVersion
        fields = (
            "id",
            "version_number",
            "label",
            "content",
            "created_by_email",
            "created_at",
        )
        read_only_fields = fields


class SaveExecutionEditSerializer(serializers.Serializer):
    """Payload for POST /skill-executions/{id}/edit/."""

    content = serializers.CharField(allow_blank=False)
    label = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")


class ApproveStepSerializer(serializers.Serializer):
    """Input for POST /api/skill-executions/{id}/approve/"""
    override_content = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        default=None,
        help_text=(
            "Optional replacement text for the current step's output. "
            "When provided, subsequent steps will use this edited version as context."
        ),
    )


class RunSkillSerializer(serializers.Serializer):
    """Input for POST /api/skills/{slug}/run/"""
    context_type = serializers.ChoiceField(
        choices=["repository", "project", "document"],
        required=True,
    )
    context_slug = serializers.SlugField(required=True)
    extra_instructions = serializers.CharField(required=False, allow_blank=True, default="")
    input_values = serializers.DictField(
        required=False,
        default=dict,
        help_text=(
            "Values for the skill's declared typed parameters, keyed by parameter key. "
            "Example: {\"framework\": \"GRI\", \"target_year\": \"2024\"}."
        ),
    )
    # Optional subset of documents to consider for this run. When provided, the
    # runner intersects this list with the context's accessible documents
    # (project linked docs / repository active docs). Omit to use the full
    # context — preserves existing behaviour.
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        required=False,
        allow_empty=True,
        default=list,
    )
    output_mode = serializers.ChoiceField(
        choices=ExecutionOutputMode.choices,
        required=False,
    )
    # Per-step document overrides for copilot workflows.  Keys are step
    # positions (as strings), values are lists of document slugs that
    # override the step's definition-time document_slugs for this run.
    step_document_overrides = serializers.DictField(
        child=serializers.ListField(child=serializers.SlugField()),
        required=False,
        default=dict,
    )
    # Copilot only. When true, the workflow pauses after every step for human
    # review (block-by-block confirmation), regardless of each step's
    # definition-time approval_required flag. Per-step approval_required is
    # still honored on top of this.
    review_each_step = serializers.BooleanField(required=False, default=False)
    table_schema = serializers.DictField(required=False)
    table_columns = serializers.ListField(
        child=serializers.CharField(max_length=120),
        required=False,
        allow_empty=False,
    )

    def validate(self, attrs):
        context_type = attrs["context_type"]
        context_slug = attrs["context_slug"]
        user = self.context["request"].user

        if context_type == "repository":
            from apps.repository.models import Repository
            try:
                repository = Repository.objects.for_user(user).get(slug=context_slug)
            except Repository.DoesNotExist:
                raise serializers.ValidationError({"context_slug": "Repository not found."})
            if not repository.can_edit(user):
                raise serializers.ValidationError(
                    {"context_slug": "No tienes permisos de edición en este repositorio."}
                )
            attrs["repository"] = repository

        elif context_type == "project":
            from apps.project.models import Project
            try:
                project = Project.objects.for_user(user).get(slug=context_slug)
            except Project.DoesNotExist:
                raise serializers.ValidationError({"context_slug": "Project not found."})
            if not project.can_edit(user):
                raise serializers.ValidationError(
                    {"context_slug": "No tienes permisos de edición en este proyecto."}
                )
            attrs["project"] = project

        elif context_type == "document":
            doc_qs = accessible_documents_for(user, [context_slug])
            doc = doc_qs.filter(slug=context_slug).first()
            if doc is None:
                raise serializers.ValidationError({"context_slug": "Document not found."})
            if not doc.can_edit(user):
                raise serializers.ValidationError(
                    {"context_slug": "No tienes permisos de edición en este documento."}
                )
            attrs["document"] = doc

        # Override or fallback: the runner is the source of truth for resolving the
        # *effective* output mode and schema. Here we only validate user-provided
        # overrides for shape, leaving the merge with skill defaults to the view.
        output_mode = attrs.get("output_mode")
        table_schema = attrs.get("table_schema") or {}
        table_columns = attrs.get("table_columns") or []

        if table_schema and not table_columns and isinstance(table_schema.get("columns"), list):
            attrs["table_columns"] = [
                c.get("key") if isinstance(c, dict) else c
                for c in table_schema["columns"]
            ]

        if output_mode == ExecutionOutputMode.TABLE:
            if table_schema:
                attrs["table_schema"] = _normalize_table_schema_or_raise(
                    table_schema, field="table_schema"
                )
            elif table_columns:
                attrs["table_schema"] = _normalize_table_schema_or_raise(
                    {"columns": [{"key": c, "label": c, "type": "text"} for c in table_columns]},
                    field="table_schema",
                )
        elif output_mode == ExecutionOutputMode.TEXT:
            if schema_has_columns(table_schema) or table_columns:
                raise serializers.ValidationError(
                    {"table_schema": "table schema fields can only be used with output_mode='table'."}
                )

        return attrs
