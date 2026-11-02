"""Serializers for template-based evaluation dashboards."""

from rest_framework import serializers

from apps.evaluation.models_template import (
    EvaluationKPITemplate,
    EvaluationPillarTemplate,
    EvaluationTemplate,
    TemplateEvaluationRun,
    TemplateEvaluationRunScore,
)
from apps.project.models import Project


class EvaluationKPITemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvaluationKPITemplate
        fields = ("id", "code", "name", "max_score")


class EvaluationPillarTemplateSerializer(serializers.ModelSerializer):
    kpis = EvaluationKPITemplateSerializer(many=True, read_only=True)

    class Meta:
        model = EvaluationPillarTemplate
        fields = ("id", "code", "name", "weight", "kpis")


class EvaluationTemplateSerializer(serializers.ModelSerializer):
    pillars = EvaluationPillarTemplateSerializer(many=True, read_only=True)

    class Meta:
        model = EvaluationTemplate
        fields = ("id", "name", "description", "methodology", "pillars", "created_at")


class TemplateEvaluationRunScoreSerializer(serializers.ModelSerializer):
    kpi_id = serializers.UUIDField(source="kpi.id", read_only=True)
    kpi_code = serializers.CharField(source="kpi.code", read_only=True)
    kpi_name = serializers.CharField(source="kpi.name", read_only=True)
    pillar_code = serializers.CharField(source="kpi.pillar.code", read_only=True)
    pillar_name = serializers.CharField(source="kpi.pillar.name", read_only=True)

    class Meta:
        model = TemplateEvaluationRunScore
        fields = (
            "id",
            "kpi_id",
            "kpi_code",
            "kpi_name",
            "pillar_code",
            "pillar_name",
            "score",
            "evidence",
        )


class TemplateEvaluationRunSerializer(serializers.ModelSerializer):
    project_id = serializers.IntegerField(source="project.id", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    project_slug = serializers.SlugField(source="project.slug", read_only=True)
    template_id = serializers.UUIDField(source="template.id", read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True)
    scores = TemplateEvaluationRunScoreSerializer(many=True, read_only=True)

    class Meta:
        model = TemplateEvaluationRun
        fields = (
            "id",
            "project_id",
            "project_name",
            "project_slug",
            "template_id",
            "template_name",
            "executed_at",
            "status",
            "metadata",
            "created_at",
            "scores",
        )


class RunEvaluationCreateSerializer(serializers.Serializer):
    """Accepts projectId or project_id, templateId or template_id (camelCase for frontend)."""
    project_id = serializers.IntegerField(required=False)
    projectId = serializers.IntegerField(required=False, allow_null=True)
    template_id = serializers.UUIDField(required=False)
    templateId = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        project_id = attrs.get("project_id") or attrs.get("projectId")
        template_id = attrs.get("template_id") or attrs.get("templateId")
        if project_id is None:
            raise serializers.ValidationError({"project_id": "Este campo es requerido."})
        if template_id is None:
            raise serializers.ValidationError({"template_id": "Este campo es requerido."})
        attrs["project_id"] = project_id
        attrs["template_id"] = template_id

        project = Project.objects.filter(pk=project_id).first()
        if not project:
            raise serializers.ValidationError({"project_id": "Proyecto no encontrado."})
        if not project.can_view(self.context["request"].user):
            raise serializers.ValidationError({"project_id": "Proyecto no encontrado."})

        if not EvaluationTemplate.objects.filter(pk=template_id).exists():
            raise serializers.ValidationError({"template_id": "Plantilla de evaluación no encontrada."})
        return attrs
