from __future__ import annotations

import os
from typing import Iterable

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.document.models import Document
from apps.project.models import Project

DEFAULT_EVALUATION_MODEL = os.environ.get("MODEL_COMPLETION", "gpt-4o-mini")


class EvaluationVisibility(models.TextChoices):
    PRIVATE = "private", _("Privada")
    SHARED = "shared", _("Compartida")
    PUBLIC = "public", _("Pública")


class EvaluationQuerySet(models.QuerySet):
    def for_user(self, user):
        from apps.user.access import public_reach_q

        if user.is_staff:
            return self
        from apps.user.access import institution_boundary_q

        return self.filter(
            (
                Q(owner=user)
                | (Q(visibility=EvaluationVisibility.PUBLIC) & public_reach_q(user))
                | Q(shares__user=user)
            )
            # La de una operación de otra institución no le llega, aunque la haya
            # creado él antes de salir de esa institución.
            & institution_boundary_q(user, "project__organization")
        ).distinct()


class Evaluation(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="evaluations",
        on_delete=models.CASCADE,
    )
    project = models.ForeignKey(
        Project,
        related_name="evaluations",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, blank=True, max_length=255)
    description = models.TextField(blank=True)
    visibility = models.CharField(
        max_length=20,
        choices=EvaluationVisibility.choices,
        default=EvaluationVisibility.PRIVATE,
    )
    system_prompt = models.TextField(
        blank=True,
        default=(
            "Eres un asistente que realiza evaluaciones estructuradas. "
            "Limítate a la información provista."
        ),
    )
    language = models.CharField(max_length=16, default="es")
    model = models.CharField(max_length=100, default=DEFAULT_EVALUATION_MODEL)
    temperature = models.FloatField(default=0.1)
    documents = models.ManyToManyField(
        Document,
        through="EvaluationDocument",
        related_name="evaluations",
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EvaluationQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("owner", "created_at")),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.owner})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def _generate_unique_slug(self) -> str:
        base = slugify(self.title) or "evaluation"
        base = base[:255]
        slug = base
        counter = 1
        while Evaluation.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            suffix = f"-{counter}"
            slug = f"{base[: 255 - len(suffix)]}{suffix}"
            counter += 1
        return slug

    def can_view(self, user) -> bool:
        from apps.user.access import within_institution

        if user.is_staff:
            return True
        if self.project_id and not within_institution(user, self.project.organization_id):
            return False
        if self.owner_id == user.id:
            return True
        from apps.user.access import public_reaches

        if self.visibility == EvaluationVisibility.PUBLIC and public_reaches(user, self.owner):
            return True
        return self.shares.filter(user=user).exists()

    def can_edit(self, user) -> bool:
        from apps.user.access import within_institution

        if user.is_staff:
            return True
        if self.project_id and not within_institution(user, self.project.organization_id):
            return False
        if self.owner_id == user.id:
            return True
        return self.shares.filter(
            user=user, role=EvaluationShareRole.EDITOR
        ).exists()

    def can_manage_shares(self, user) -> bool:
        return user.is_staff or self.owner_id == user.id

    def base_documents(self) -> Iterable[Document]:
        """
        Returns the documents explicitly attached to the evaluation.
        """
        return Document.objects.filter(evaluation_documents__evaluation=self)


class EvaluationDocument(models.Model):
    evaluation = models.ForeignKey(
        Evaluation,
        related_name="evaluation_documents",
        on_delete=models.CASCADE,
    )
    document = models.ForeignKey(
        Document,
        related_name="evaluation_documents",
        on_delete=models.CASCADE,
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="added_evaluation_documents",
        on_delete=models.SET_NULL,
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("evaluation", "document")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.evaluation_id}-{self.document_id}"


class EvaluationShareRole(models.TextChoices):
    VIEWER = "viewer", _("Viewer")
    EDITOR = "editor", _("Editor")


class EvaluationShare(models.Model):
    evaluation = models.ForeignKey(
        Evaluation,
        related_name="shares",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="evaluation_shares",
        on_delete=models.CASCADE,
    )
    role = models.CharField(
        max_length=20,
        choices=EvaluationShareRole.choices,
        default=EvaluationShareRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("evaluation", "user")
        ordering = ("evaluation", "user")

    def __str__(self) -> str:
        return f"{self.evaluation_id}-{self.user_id}-{self.role}"


class EvaluationPillar(models.Model):
    evaluation = models.ForeignKey(
        Evaluation,
        related_name="pillars",
        on_delete=models.CASCADE,
    )
    title = models.CharField(max_length=255)
    context_instructions = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "id")
        unique_together = ("evaluation", "position")

    def __str__(self) -> str:
        return f"{self.title} ({self.evaluation_id})"


class MetricResponseType(models.TextChoices):
    QUANTITATIVE = "quantitative", _("Cuantitativo")
    QUALITATIVE = "qualitative", _("Cualitativo")


class EvaluationMetric(models.Model):
    pillar = models.ForeignKey(
        EvaluationPillar,
        related_name="metrics",
        on_delete=models.CASCADE,
    )
    title = models.CharField(max_length=255)
    instructions = models.TextField()
    criteria = models.TextField(blank=True)
    response_type = models.CharField(
        max_length=20,
        choices=MetricResponseType.choices,
        default=MetricResponseType.QUALITATIVE,
    )
    scale_min = models.FloatField(null=True, blank=True)
    scale_max = models.FloatField(null=True, blank=True)
    scale_label_min = models.CharField(max_length=255, blank=True)
    scale_label_max = models.CharField(max_length=255, blank=True)
    expected_units = models.CharField(max_length=255, blank=True)
    linked_skill_slug = models.SlugField(
        null=True,
        blank=True,
        help_text="Optional skill to pre-populate this metric's response.",
    )
    position = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "id")
        unique_together = ("pillar", "position")

    def __str__(self) -> str:
        return f"{self.title} ({self.pillar_id})"


class EvaluationRunStatus(models.TextChoices):
    PENDING = "pending", _("Pendiente")
    RUNNING = "running", _("En ejecución")
    COMPLETED = "completed", _("Completado")
    FAILED = "failed", _("Fallido")


class EvaluationRun(models.Model):
    evaluation = models.ForeignKey(
        Evaluation,
        related_name="runs",
        on_delete=models.CASCADE,
    )
    project = models.ForeignKey(
        Project,
        related_name="evaluation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="evaluation_runs",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=20,
        choices=EvaluationRunStatus.choices,
        default=EvaluationRunStatus.PENDING,
    )
    model = models.CharField(max_length=100, default=DEFAULT_EVALUATION_MODEL)
    language = models.CharField(max_length=16, default="es")
    temperature = models.FloatField(default=0.1)
    instructions_override = models.TextField(blank=True)
    document_snapshot = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("evaluation", "created_at")),
            models.Index(fields=("owner", "created_at")),
        ]

    def __str__(self) -> str:
        return f"Run {self.id} ({self.evaluation_id})"


class PillarEvaluationResult(models.Model):
    run = models.ForeignKey(
        EvaluationRun,
        related_name="pillar_results",
        on_delete=models.CASCADE,
    )
    pillar = models.ForeignKey(
        EvaluationPillar,
        related_name="pillar_results",
        on_delete=models.CASCADE,
    )
    position = models.PositiveIntegerField(default=1)
    summary = models.TextField(blank=True)
    chunk_ids = ArrayField(
        base_field=models.IntegerField(),
        default=list,
        blank=True,
    )
    sources = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("position", "id")

    def __str__(self) -> str:
        return f"PillarResult {self.id}"


class MetricEvaluationResult(models.Model):
    pillar_result = models.ForeignKey(
        PillarEvaluationResult,
        related_name="metric_results",
        on_delete=models.CASCADE,
    )
    metric = models.ForeignKey(
        EvaluationMetric,
        related_name="metric_results",
        on_delete=models.CASCADE,
    )
    response_type = models.CharField(
        max_length=20,
        choices=MetricResponseType.choices,
    )
    response_text = models.TextField(blank=True)
    response_value = models.FloatField(null=True, blank=True)
    chunk_ids = ArrayField(
        base_field=models.IntegerField(),
        default=list,
        blank=True,
    )
    sources = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    position = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("position", "id")

    def __str__(self) -> str:
        return f"MetricResult {self.id}"


# Import template-based evaluation models for dashboards
from apps.evaluation.models_template import (  # noqa: E402, F401
    EvaluationKPITemplate,
    EvaluationPillarTemplate,
    EvaluationTemplate,
    TemplateEvaluationRun,
    TemplateEvaluationRunScore,
    TemplateEvaluationRunStatus,
)

