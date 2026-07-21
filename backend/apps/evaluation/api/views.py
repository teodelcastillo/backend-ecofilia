from __future__ import annotations

from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.evaluation.api.permissions import EvaluationAccessPermission
from apps.evaluation.api.serializers import (
    ApplySkillEvidenceSerializer,
    EvaluationRunCreateSerializer,
    EvaluationRunSerializer,
    EvaluationSerializer,
    EvaluationShareRoleUpdateSerializer,
    EvaluationShareSerializer,
    EvaluationShareWriteSerializer,
    EvaluationWriteSerializer,
    MetricEvaluationResultSerializer,
)
from apps.evaluation.models import (
    Evaluation,
    EvaluationDocument,
    EvaluationPillar,
    EvaluationRun,
    EvaluationShare,
    PillarEvaluationResult,
)
from apps.evaluation.services import apply_skill_execution_to_metric
from apps.evaluation.tasks import run_evaluation_task


class EvaluationViewSet(viewsets.ModelViewSet):
    queryset = Evaluation.objects.none()
    serializer_class = EvaluationSerializer
    permission_classes = [IsAuthenticated, EvaluationAccessPermission]
    lookup_field = "slug"

    def get_queryset(self):
        user = self.request.user
        qs = (
            Evaluation.objects.for_user(user)
            .select_related("owner", "project")
            .prefetch_related(
                Prefetch(
                    "evaluation_documents",
                    queryset=EvaluationDocument.objects.select_related("document"),
                ),
                Prefetch(
                    "pillars",
                    queryset=EvaluationPillar.objects.prefetch_related("metrics"),
                ),
                Prefetch(
                    "shares",
                    queryset=EvaluationShare.objects.select_related("user"),
                ),
            )
            .distinct()
        )
        # `?project=<slug>` lets project detail pages cheaply scope evaluations
        # to the current project without hand-rolling a per-project endpoint.
        project_slug = self.request.query_params.get("project")
        if project_slug:
            qs = qs.filter(project__slug=project_slug)
        return qs

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return EvaluationWriteSerializer
        return EvaluationSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def create(self, request, *args, **kwargs):
        if getattr(request.user, "is_restricted", False):
            raise PermissionDenied(
                "Your organization cannot create evaluations; contact Ecofilia."
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        evaluation = serializer.save(owner=request.user)
        output = EvaluationSerializer(
            evaluation, context=self.get_serializer_context()
        )
        headers = self.get_success_headers(output.data)
        return Response(output.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        output = EvaluationSerializer(
            self.get_object(), context=self.get_serializer_context()
        )
        return Response(output.data)

    def perform_update(self, serializer):
        evaluation = self.get_object()
        if not evaluation.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para editar esta evaluación.")
        serializer.save()

    def perform_destroy(self, instance):
        if not instance.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para eliminar esta evaluación.")
        instance.delete()

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="shares",
        url_name="shares",
    )
    def shares(self, request, slug=None):
        evaluation = self.get_object()
        self._ensure_share_manager(evaluation)
        if request.method == "GET":
            serializer = EvaluationShareSerializer(
                evaluation.shares.select_related("user"), many=True
            )
            return Response(serializer.data)

        serializer = EvaluationShareWriteSerializer(
            data=request.data,
            context={"evaluation": evaluation},
        )
        serializer.is_valid(raise_exception=True)
        share, _ = EvaluationShare.objects.update_or_create(
            evaluation=evaluation,
            user=serializer.validated_data["user"],
            defaults={"role": serializer.validated_data["role"]},
        )
        return Response(
            EvaluationShareSerializer(share).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"shares/(?P<share_id>[^/]+)",
        url_name="share-detail",
    )
    def share_detail(self, request, slug=None, share_id=None):
        evaluation = self.get_object()
        self._ensure_share_manager(evaluation)
        share = get_object_or_404(EvaluationShare, evaluation=evaluation, pk=share_id)

        if request.method == "PATCH":
            serializer = EvaluationShareRoleUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            share.role = serializer.validated_data["role"]
            share.save(update_fields=["role"])
            return Response(EvaluationShareSerializer(share).data)

        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="runs",
        url_name="runs",
    )
    def runs(self, request, slug=None):
        evaluation = self.get_object()
        if request.method == "GET":
            runs = self._run_queryset().filter(evaluation=evaluation)
            serializer = EvaluationRunSerializer(runs, many=True)
            return Response(serializer.data)

        if not evaluation.can_edit(request.user):
            raise PermissionDenied("No puedes ejecutar esta evaluación.")

        serializer = EvaluationRunCreateSerializer(
            data=request.data,
            context={"request": request, "evaluation": evaluation},
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        project = validated.get("project_instance") or evaluation.project
        documents_snapshot = serializer.create_run_documents(evaluation, validated)
        run = EvaluationRun.objects.create(
            evaluation=evaluation,
            project=project,
            owner=request.user,
            model=validated.get("model") or evaluation.model,
            language=validated.get("language") or evaluation.language,
            temperature=validated.get("temperature") or evaluation.temperature,
            instructions_override=validated.get("instructions_override", ""),
            document_snapshot=documents_snapshot,
        )
        run_evaluation_task.delay(run.id)
        output = EvaluationRunSerializer(
            self._run_queryset().get(pk=run.pk)
        )
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["get"],
        url_path=r"runs/(?P<run_id>[^/]+)",
        url_name="run-detail",
    )
    def run_detail(self, request, slug=None, run_id=None):
        evaluation = self.get_object()
        run = get_object_or_404(self._run_queryset(), evaluation=evaluation, pk=run_id)
        if not evaluation.can_view(request.user):
            raise PermissionDenied("No tienes permisos para ver esta ejecución.")
        serializer = EvaluationRunSerializer(run)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"runs/(?P<run_id>[^/]+)/apply-skill-evidence",
        url_name="apply-skill-evidence",
    )
    def apply_skill_evidence(self, request, slug=None, run_id=None):
        evaluation = self.get_object()
        if not evaluation.can_edit(request.user):
            raise PermissionDenied("No tienes permisos para editar esta evaluación.")

        run = get_object_or_404(self._run_queryset(), evaluation=evaluation, pk=run_id)
        serializer = ApplySkillEvidenceSerializer(
            data=request.data,
            context={"request": request, "run": run},
        )
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        try:
            metric_result = apply_skill_execution_to_metric(
                run=run,
                metric_id=validated["metric_id"],
                execution=validated["execution"],
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(MetricEvaluationResultSerializer(metric_result).data)

    def _ensure_share_manager(self, evaluation: Evaluation):
        if not evaluation.can_manage_shares(self.request.user):
            raise PermissionDenied("No puedes administrar los permisos de esta evaluación.")

    def _run_queryset(self):
        return EvaluationRun.objects.select_related("evaluation", "project", "owner").prefetch_related(
            Prefetch(
                "pillar_results",
                queryset=PillarEvaluationResult.objects.prefetch_related("metric_results"),
            )
        )

