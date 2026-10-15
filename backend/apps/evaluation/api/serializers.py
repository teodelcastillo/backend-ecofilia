from __future__ import annotations

from typing import Iterable, List, Sequence

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import serializers

from apps.user.sharing import resolve_share_target

from apps.document.models import Document
from apps.document.services import accessible_documents_for
from apps.evaluation.models import (
    Evaluation,
    EvaluationDocument,
    EvaluationMetric,
    EvaluationPillar,
    EvaluationRun,
    EvaluationShare,
    EvaluationShareRole,
    MetricEvaluationResult,
    MetricResponseType,
    PillarEvaluationResult,
)
from apps.project.models import Project

User = get_user_model()


class EvaluationDocumentSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(source="document.slug", read_only=True)
    name = serializers.CharField(source="document.name", read_only=True)

    class Meta:
        model = EvaluationDocument
        fields = ("id", "slug", "name", "note", "created_at")
        read_only_fields = fields


class EvaluationMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvaluationMetric
        fields = (
            "id",
            "title",
            "instructions",
            "criteria",
            "response_type",
            "scale_min",
            "scale_max",
            "scale_label_min",
            "scale_label_max",
            "expected_units",
            "linked_skill_slug",
            "position",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")


class EvaluationPillarSerializer(serializers.ModelSerializer):
    metrics = EvaluationMetricSerializer(many=True, read_only=True)

    class Meta:
        model = EvaluationPillar
        fields = (
            "id",
            "title",
            "context_instructions",
            "position",
            "metrics",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "metrics", "created_at", "updated_at")


class EvaluationSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    project_slug = serializers.SlugField(source="project.slug", read_only=True)
    can_edit = serializers.SerializerMethodField()
    can_manage_shares = serializers.SerializerMethodField()
    documents = EvaluationDocumentSerializer(
        source="evaluation_documents", many=True, read_only=True
    )
    pillars = EvaluationPillarSerializer(many=True, read_only=True)

    class Meta:
        model = Evaluation
        fields = (
            "id",
            "slug",
            "title",
            "description",
            "visibility",
            "system_prompt",
            "language",
            "model",
            "temperature",
            "is_active",
            "project",
            "project_slug",
            "owner",
            "owner_email",
            "documents",
            "pillars",
            "can_edit",
            "can_manage_shares",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "slug",
            "owner",
            "owner_email",
            "project",
            "project_slug",
            "documents",
            "pillars",
            "can_edit",
            "can_manage_shares",
            "created_at",
            "updated_at",
        )

    def get_can_edit(self, obj):
        request = self.context.get("request")
        return bool(request and obj.can_edit(request.user))

    def get_can_manage_shares(self, obj):
        request = self.context.get("request")
        return bool(request and obj.can_manage_shares(request.user))


class EvaluationMetricInputSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    instructions = serializers.CharField()
    criteria = serializers.CharField(required=False, allow_blank=True)
    response_type = serializers.ChoiceField(
        choices=MetricResponseType.choices,
        default=MetricResponseType.QUALITATIVE,
    )
    scale_min = serializers.FloatField(required=False, allow_null=True)
    scale_max = serializers.FloatField(required=False, allow_null=True)
    scale_label_min = serializers.CharField(required=False, allow_blank=True)
    scale_label_max = serializers.CharField(required=False, allow_blank=True)
    expected_units = serializers.CharField(required=False, allow_blank=True)
    linked_skill_slug = serializers.SlugField(required=False, allow_null=True, allow_blank=True)
    position = serializers.IntegerField(required=False, min_value=1)


class EvaluationPillarInputSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    context_instructions = serializers.CharField(required=False, allow_blank=True)
    position = serializers.IntegerField(required=False, min_value=1)
    metrics = EvaluationMetricInputSerializer(many=True, allow_empty=True)


class EvaluationWriteSerializer(EvaluationSerializer):
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        allow_empty=True,
        required=False,
        write_only=True,
    )
    pillars = EvaluationPillarInputSerializer(
        many=True,
        required=False,
        write_only=True,
    )
    project_slug = serializers.SlugField(
        write_only=True, required=False, allow_null=True
    )

    class Meta(EvaluationSerializer.Meta):
        fields = EvaluationSerializer.Meta.fields + ("document_slugs", "pillars", "project_slug")

    def validate_project_slug(self, slug):
        if not slug:
            return None
        try:
            project = Project.objects.get(slug=slug)
        except Project.DoesNotExist as exc:
            raise serializers.ValidationError("Proyecto no encontrado.") from exc
        request = self.context["request"]
        if not project.can_view(request.user):
            raise serializers.ValidationError("No tienes acceso a este proyecto.")
        self.context["project_instance"] = project
        return slug

    def validate_document_slugs(self, slugs):
        if not slugs:
            return []
        docs = accessible_documents_for(self.context["request"].user, slugs)
        found = set(docs.values_list("slug", flat=True))
        missing = [slug for slug in slugs if slug not in found]
        if missing:
            raise serializers.ValidationError(
                f"Documentos no encontrados o sin permisos: {', '.join(missing)}"
            )
        self.context["validated_documents"] = list(docs)
        return slugs

    @transaction.atomic
    def create(self, validated_data):
        document_slugs = validated_data.pop("document_slugs", [])
        pillars = validated_data.pop("pillars", [])
        project_slug = validated_data.pop("project_slug", serializers.empty)
        if project_slug is not serializers.empty:
            if project_slug:
                validated_data["project"] = self.context.get("project_instance")
            else:
                validated_data["project"] = None
        evaluation = Evaluation.objects.create(**validated_data)
        self._sync_documents(evaluation, document_slugs)
        self._sync_pillars(evaluation, pillars)
        return evaluation

    @transaction.atomic
    def update(self, instance, validated_data):
        document_slugs = validated_data.pop("document_slugs", None)
        pillars = validated_data.pop("pillars", None)
        project_slug = validated_data.pop("project_slug", serializers.empty)
        if project_slug is not serializers.empty:
            if project_slug:
                instance.project = self.context.get("project_instance")
            else:
                instance.project = None
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if document_slugs is not None:
            instance.evaluation_documents.all().delete()
            self._sync_documents(instance, document_slugs)
        if pillars is not None:
            instance.pillars.all().delete()
            self._sync_pillars(instance, pillars)
        return instance

    def _sync_documents(self, evaluation: Evaluation, slugs: Sequence[str]):
        if not slugs:
            return
        documents = self.context.get("validated_documents")
        if documents is None:
            documents = list(Document.objects.filter(slug__in=slugs))
        for doc in documents:
            EvaluationDocument.objects.get_or_create(
                evaluation=evaluation,
                document=doc,
                defaults={"added_by": evaluation.owner},
            )

    def _sync_pillars(self, evaluation: Evaluation, pillars_data: Sequence[dict]):
        if not pillars_data:
            return
        for idx, pillar_data in enumerate(pillars_data, start=1):
            metrics_data = pillar_data.pop("metrics", [])
            position = pillar_data.get("position") or idx
            payload = {
                key: value
                for key, value in pillar_data.items()
                if key in {"title", "context_instructions"}
            }
            pillar = EvaluationPillar.objects.create(
                evaluation=evaluation,
                position=position,
                **payload,
            )
            self._sync_metrics(pillar, metrics_data)

    def _sync_metrics(self, pillar: EvaluationPillar, metrics_data: Sequence[dict]):
        for idx, metric_data in enumerate(metrics_data, start=1):
            position = metric_data.get("position") or idx
            payload = {
                key: value
                for key, value in metric_data.items()
                if key
                in {
                    "title",
                    "instructions",
                    "criteria",
                    "response_type",
                    "scale_min",
                    "scale_max",
                    "scale_label_min",
                    "scale_label_max",
                    "expected_units",
                    "linked_skill_slug",
                }
            }
            linked_skill_slug = payload.pop("linked_skill_slug", None) or None
            if linked_skill_slug == "":
                linked_skill_slug = None
            EvaluationMetric.objects.create(
                pillar=pillar,
                position=position,
                linked_skill_slug=linked_skill_slug,
                **payload,
            )


class EvaluationShareSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = EvaluationShare
        fields = ("id", "user", "user_email", "role", "created_at")
        read_only_fields = ("id", "user_email", "created_at")


class EvaluationShareRoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=EvaluationShareRole.choices)


class EvaluationShareWriteSerializer(serializers.Serializer):
    user_email = serializers.EmailField()
    role = serializers.ChoiceField(choices=EvaluationShareRole.choices)

    def validate(self, attrs):
        """Valida el email y obtiene el usuario"""
        email = attrs.get('user_email')
        user = resolve_share_target(self.context["request"].user, email)
        
        evaluation = self.context.get("evaluation")
        if evaluation and user == evaluation.owner:
            raise serializers.ValidationError({
                'user_email': "No puedes compartir la evaluación contigo mismo."
            })
        
        # Reemplazar user_email con user para que la vista lo use
        attrs['user'] = user
        return attrs


class MetricEvaluationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = MetricEvaluationResult
        fields = (
            "id",
            "metric",
            "response_type",
            "response_text",
            "response_value",
            "chunk_ids",
            "sources",
            "metadata",
            "position",
            "created_at",
        )
        read_only_fields = fields


class PillarEvaluationResultSerializer(serializers.ModelSerializer):
    metric_results = MetricEvaluationResultSerializer(many=True, read_only=True)

    class Meta:
        model = PillarEvaluationResult
        fields = (
            "id",
            "pillar",
            "position",
            "summary",
            "chunk_ids",
            "sources",
            "metadata",
            "created_at",
            "metric_results",
        )
        read_only_fields = fields


class EvaluationRunSerializer(serializers.ModelSerializer):
    pillar_results = PillarEvaluationResultSerializer(
        many=True, read_only=True
    )

    class Meta:
        model = EvaluationRun
        fields = (
            "id",
            "evaluation",
            "project",
            "owner",
            "status",
            "model",
            "language",
            "temperature",
            "instructions_override",
            "document_snapshot",
            "metadata",
            "started_at",
            "finished_at",
            "error_message",
            "created_at",
            "pillar_results",
        )
        read_only_fields = (
            "id",
            "evaluation",
            "owner",
            "status",
            "document_snapshot",
            "metadata",
            "started_at",
            "finished_at",
            "error_message",
            "created_at",
            "pillar_results",
        )


class EvaluationRunCreateSerializer(serializers.Serializer):
    project_slug = serializers.SlugField(required=False, allow_null=True)
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        required=False,
        allow_empty=True,
    )
    model = serializers.CharField(required=False)
    language = serializers.CharField(required=False)
    temperature = serializers.FloatField(required=False)
    instructions_override = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        request = self.context["request"]
        evaluation: Evaluation = self.context["evaluation"]
        project = None
        if attrs.get("project_slug"):
            try:
                project = Project.objects.get(slug=attrs["project_slug"])
            except Project.DoesNotExist as exc:
                raise serializers.ValidationError(
                    {"project_slug": "Proyecto no encontrado."}
                ) from exc
            if not project.can_view(request.user):
                raise serializers.ValidationError(
                    {"project_slug": "No tienes acceso a este proyecto."}
                )
        attrs["project_instance"] = project

        slugs = attrs.get("document_slugs") or []
        if slugs:
            docs = accessible_documents_for(request.user, slugs)
            found = set(docs.values_list("slug", flat=True))
            missing = [slug for slug in slugs if slug not in found]
            if missing:
                raise serializers.ValidationError(
                    {"document_slugs": f"Documentos no permitidos: {', '.join(missing)}"}
                )
            attrs["validated_documents"] = list(docs)
        else:
            attrs["validated_documents"] = []

        if not slugs and not project and not evaluation.evaluation_documents.exists():
            raise serializers.ValidationError(
                {
                    "document_slugs": "Debe haber al menos un documento, ya sea en la evaluación, proyecto o payload."
                }
            )
        return attrs

    def create_run_documents(self, evaluation: Evaluation, validated_data) -> List[dict]:
        docs = validated_data.get("validated_documents") or []
        if not docs:
            if validated_data.get("project_instance"):
                project_docs = validated_data["project_instance"].project_documents.select_related("document")
                docs = [pd.document for pd in project_docs]
            else:
                docs = [ed.document for ed in evaluation.evaluation_documents.select_related("document")]
        serialized = []
        seen = set()
        for doc in docs:
            if doc.slug in seen:
                continue
            seen.add(doc.slug)
            serialized.append(
                {
                    "id": doc.id,
                    "slug": doc.slug,
                    "name": doc.name,
                }
            )
        return serialized


class ApplySkillEvidenceSerializer(serializers.Serializer):
    execution_id = serializers.IntegerField(min_value=1)
    metric_id = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        from apps.skill.models import SkillExecution, ExecutionStatus

        request = self.context["request"]
        run: EvaluationRun = self.context["run"]
        try:
            execution = SkillExecution.objects.select_related("skill").get(
                pk=attrs["execution_id"],
                owner=request.user,
            )
        except SkillExecution.DoesNotExist as exc:
            raise serializers.ValidationError(
                {"execution_id": "Skill execution not found."}
            ) from exc

        if execution.status != ExecutionStatus.COMPLETED:
            raise serializers.ValidationError(
                {"execution_id": "Skill execution must be completed."}
            )

        metric_exists = MetricEvaluationResult.objects.filter(
            pillar_result__run=run,
            metric_id=attrs["metric_id"],
        ).exists()
        if not metric_exists:
            raise serializers.ValidationError(
                {"metric_id": "Metric not found in this evaluation run."}
            )

        attrs["execution"] = execution
        return attrs
