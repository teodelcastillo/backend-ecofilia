from __future__ import annotations

from django.db.models import Prefetch, Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.skill.access import (
    executions_queryset_for_user,
    user_can_mutate_execution,
    user_can_view_execution,
)

from apps.skill.api.serializers import (
    ApproveStepSerializer,
    RunSkillSerializer,
    SaveExecutionEditSerializer,
    SkillExecutionSerializer,
    SkillExecutionVersionSerializer,
    SkillSerializer,
    SkillWriteSerializer,
)
from apps.skill.models import (
    ExecutionOutputMode,
    ExecutionStatus,
    Skill,
    SkillExecution,
    SkillExecutionVersion,
    SkillStep,
    SkillType,
)
from apps.skill.table_schema import schema_has_columns
from apps.skill.services import approve_step, regenerate_step
from apps.skill.tasks import run_skill_task


class SkillViewSet(viewsets.ModelViewSet):
    """
    CRUD for Skills + the run action.
    Returns: Ecofilia templates (owner=null) + the requesting user's own skills.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = SkillSerializer
    lookup_field = "slug"

    def get_queryset(self):
        user = self.request.user
        qs = (
            Skill.objects
            .filter(Q(owner__isnull=True) | Q(owner=user))
            .prefetch_related(
                Prefetch("steps", queryset=SkillStep.objects.order_by("position"))
            )
            .select_related("owner")
        )
        # Restricted orgs (e.g. CAF) only see the skills Ecofilia enabled for them.
        if getattr(user, "is_restricted", False):
            qs = qs.filter(enabled_for_organizations=user.organization)
        skill_type = self.request.query_params.get("skill_type")
        if skill_type:
            qs = qs.filter(skill_type=skill_type)
        context = self.request.query_params.get("context")
        if context:
            # Match skills whose allowed_contexts includes this context OR "any"
            qs = qs.filter(
                Q(allowed_contexts__contains=[context]) |
                Q(allowed_contexts__contains=["any"])
            )
        context_slug = self.request.query_params.get("context_slug")
        if context == "repository" and context_slug:
            from apps.repository.models import Repository
            try:
                repository = Repository.objects.for_user(user).get(slug=context_slug)
            except Repository.DoesNotExist:
                return qs.none()
            qs = qs.filter(enabled_repositories=repository).distinct()
        elif context == "project" and context_slug:
            from apps.project.models import Project
            try:
                project = Project.objects.for_user(user).get(slug=context_slug)
            except Project.DoesNotExist:
                return qs.none()
            qs = qs.filter(enabled_projects=project).distinct()
        return qs

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return SkillWriteSerializer
        return SkillSerializer

    def perform_create(self, serializer):
        if getattr(self.request.user, "is_restricted", False):
            raise PermissionDenied(
                "Your organization cannot create skills; contact Ecofilia."
            )
        serializer.save(owner=self.request.user, is_template=False)

    def perform_update(self, serializer):
        skill = self.get_object()
        if not skill.can_edit(self.request.user):
            raise PermissionDenied("You cannot edit this skill.")
        serializer.save()

    def perform_destroy(self, instance):
        if not instance.can_edit(self.request.user):
            raise PermissionDenied("You cannot delete this skill.")
        instance.delete()

    @action(detail=True, methods=["post"], url_path="run")
    def run(self, request, slug=None):
        """
        Execute a skill against a context.
        QUICK skills run synchronously and return the full output immediately.
        COPILOT skills are dispatched asynchronously and return the execution ID.
        """
        skill = self.get_object()

        serializer = RunSkillSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Validate the requested context type is allowed by this skill
        context_type = data["context_type"]
        if "any" not in skill.allowed_contexts and context_type not in skill.allowed_contexts:
            return Response(
                {"detail": f"This skill does not support '{context_type}' context. "
                           f"Allowed: {skill.allowed_contexts}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._skill_enabled_for_context(skill, context_type, data):
            context_label = {"repository": "repository", "project": "project"}.get(context_type, "context")
            return Response(
                {"detail": f"This skill is not enabled for this {context_label}. Add it via the Skills panel settings."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            effective_output_mode, effective_table_schema = self._resolve_effective_output(
                skill=skill,
                run_output_mode=data.get("output_mode"),
                run_table_schema=data.get("table_schema") or {},
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Per-run document filter — narrows the context's documents to the
        # explicit selection the user is acting on. Stored in metadata so
        # resolve_documents() can pick it up at execution time.
        requested_doc_slugs = [s for s in (data.get("document_slugs") or []) if s]

        execution = SkillExecution.objects.create(
            skill=skill,
            owner=request.user,
            repository=data.get("repository"),
            project=data.get("project"),
            document=data.get("document"),
            extra_instructions=data.get("extra_instructions", ""),
            input_values=data.get("input_values") or {},
            output_mode=effective_output_mode,
            metadata={
                "table_columns": [
                    col.get("key")
                    for col in (effective_table_schema.get("columns") or [])
                    if isinstance(col, dict) and col.get("key")
                ],
                "table_schema": effective_table_schema,
                "document_slugs_filter": requested_doc_slugs,
                "step_document_overrides": data.get("step_document_overrides") or {},
                "review_each_step": bool(data.get("review_each_step")),
            },
            status=ExecutionStatus.PENDING,
        )

        if skill.skill_type == SkillType.QUICK:
            # Run synchronously — result is ready when this returns
            run_skill_task(execution.id)
            execution.refresh_from_db()
            return Response(
                SkillExecutionSerializer(execution).data,
                status=status.HTTP_200_OK,
            )
        else:
            # Dispatch async for multi-step copilot
            run_skill_task.delay(execution.id)
            return Response(
                SkillExecutionSerializer(execution).data,
                status=status.HTTP_202_ACCEPTED,
            )

    def _skill_enabled_for_context(self, skill: Skill, context_type: str, data: dict) -> bool:
        if context_type == "document":
            return True
        if context_type == "repository":
            repository = data.get("repository")
            return bool(repository and repository.enabled_skills.filter(pk=skill.pk).exists())
        if context_type == "project":
            project = data.get("project")
            return bool(project and project.enabled_skills.filter(pk=skill.pk).exists())
        return False

    def _resolve_effective_output(
        self,
        *,
        skill: Skill,
        run_output_mode,
        run_table_schema: dict,
    ) -> tuple[str, dict]:
        """
        Resolve the effective output_mode and table_schema for a run.

        Precedence:
          1. Explicit `output_mode` in the run payload (with optional override schema).
          2. Skill `default_output_mode` (and its persisted `table_schema`).
          3. Defaults to TEXT.

        For Copilots the *skill-level* table_schema is intentionally ignored;
        each step decides its own output_mode/table_schema, so we only honor a
        run-level override when the user explicitly requests a single tabular
        output for the whole copilot.
        """
        if run_output_mode:
            mode = run_output_mode
        else:
            mode = skill.default_output_mode or ExecutionOutputMode.TEXT

        if mode == ExecutionOutputMode.TABLE:
            if skill.skill_type == SkillType.COPILOT and not run_table_schema:
                raise ValueError(
                    "Copilots produce tabular output per step. "
                    "Either configure step output_mode='table' on the skill, or "
                    "send a run-level table_schema to consolidate the result."
                )
            schema = run_table_schema or skill.table_schema or {}
            if not schema_has_columns(schema):
                raise ValueError(
                    "Cannot run skill in table mode: no table_schema is configured."
                )
            return mode, schema
        return ExecutionOutputMode.TEXT, {}


class SkillExecutionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    Read-only view of skill executions for the current user and shared contexts.
    Supports filtering by skill, status, repository, project.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = SkillExecutionSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            executions_queryset_for_user(user)
            .select_related("skill", "repository", "project", "document")
        )
        if skill_slug := self.request.query_params.get("skill"):
            qs = qs.filter(skill__slug=skill_slug)
        if repo_slug := self.request.query_params.get("repository"):
            qs = qs.filter(repository__slug=repo_slug)
        if project_slug := self.request.query_params.get("project"):
            qs = qs.filter(project__slug=project_slug)
        if status_filter := self.request.query_params.get("status"):
            qs = qs.filter(status=status_filter)
        return qs

    def get_object(self):
        execution = super().get_object()
        if not user_can_view_execution(self.request.user, execution):
            raise PermissionDenied("No tienes permisos para ver esta ejecución.")
        return execution

    def perform_destroy(self, instance):
        if not user_can_mutate_execution(self.request.user, instance):
            raise PermissionDenied("No tienes permisos para eliminar esta ejecución.")
        instance.delete()

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """
        Approve the current awaiting step and continue the run.

        Optionally accepts ``override_content`` to replace the step's text
        output before resuming — letting the consultant edit the draft.

        POST /api/skill-executions/{id}/approve/
        Body: { "override_content": "..." }   (optional)
        """
        execution = self.get_object()
        if not user_can_mutate_execution(request.user, execution):
            raise PermissionDenied("No tienes permisos para modificar esta ejecución.")
        serializer = ApproveStepSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            execution = approve_step(
                execution,
                override_content=serializer.validated_data.get("override_content"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        run_skill_task.delay(execution.id)
        return Response(SkillExecutionSerializer(execution).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path="regenerate-step")
    def regenerate_step_action(self, request, pk=None):
        """
        Discard the last completed step and re-run it from scratch.

        POST /api/skill-executions/{id}/regenerate-step/
        """
        execution = self.get_object()
        if not user_can_mutate_execution(request.user, execution):
            raise PermissionDenied("No tienes permisos para modificar esta ejecución.")
        try:
            execution = regenerate_step(execution)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        run_skill_task.delay(execution.id)
        return Response(SkillExecutionSerializer(execution).data, status=status.HTTP_202_ACCEPTED)

    # ------------------------------------------------------------------
    # Editable output + version history
    # ------------------------------------------------------------------

    @action(detail=True, methods=["get", "post"], url_path="versions")
    def versions(self, request, pk=None):
        """
        GET  /skill-executions/{id}/versions/  → list saved versions (newest first).
        POST /skill-executions/{id}/versions/  → save the current edit as a new
            version. Body: { "content": str, "label?": str }.
        """
        execution = self.get_object()

        if request.method == "GET":
            qs = execution.versions.select_related("created_by").order_by("-version_number")
            data = SkillExecutionVersionSerializer(qs, many=True).data
            return Response(data)

        if not user_can_mutate_execution(request.user, execution):
            raise PermissionDenied("No tienes permisos para editar esta ejecución.")

        serializer = SaveExecutionEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        content = serializer.validated_data["content"]
        label = serializer.validated_data.get("label", "")

        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            execution = (
                SkillExecution.objects.select_for_update().get(pk=execution.pk)
            )
            last = (
                execution.versions.order_by("-version_number").first()
            )
            next_number = (last.version_number + 1) if last else 1
            version = SkillExecutionVersion.objects.create(
                execution=execution,
                version_number=next_number,
                label=label,
                content=content,
                created_by=request.user,
            )
            execution.edited_output = content
            execution.edited_at = timezone.now()
            execution.edited_by = request.user
            execution.save(update_fields=["edited_output", "edited_at", "edited_by"])

        return Response(
            {
                "version": SkillExecutionVersionSerializer(version).data,
                "execution": SkillExecutionSerializer(execution).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path=r"versions/(?P<version_number>\d+)/restore",
    )
    def restore_version(self, request, pk=None, version_number=None):
        """
        Restore a previous version into the current edited_output.

        Restoring also creates a new version on top so the history is
        append-only — undoing a restore is just another restore.
        """
        execution = self.get_object()
        if not user_can_mutate_execution(request.user, execution):
            raise PermissionDenied("No tienes permisos para editar esta ejecución.")

        try:
            version = execution.versions.get(version_number=int(version_number))
        except SkillExecutionVersion.DoesNotExist:
            return Response(
                {"detail": "Version not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        from django.db import transaction
        from django.utils import timezone

        with transaction.atomic():
            execution = (
                SkillExecution.objects.select_for_update().get(pk=execution.pk)
            )
            last = execution.versions.order_by("-version_number").first()
            next_number = (last.version_number + 1) if last else 1
            new_version = SkillExecutionVersion.objects.create(
                execution=execution,
                version_number=next_number,
                label=f"Restaurada desde v{version.version_number}",
                content=version.content,
                created_by=request.user,
            )
            execution.edited_output = version.content
            execution.edited_at = timezone.now()
            execution.edited_by = request.user
            execution.save(update_fields=["edited_output", "edited_at", "edited_by"])

        return Response(
            {
                "version": SkillExecutionVersionSerializer(new_version).data,
                "execution": SkillExecutionSerializer(execution).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reset-edit")
    def reset_edit(self, request, pk=None):
        """
        Drop the current edited copy and fall back to the raw AI output.

        Note: this does NOT delete the version history — only clears the
        live edited_output. Saved versions remain available for restore.
        """
        execution = self.get_object()
        if not user_can_mutate_execution(request.user, execution):
            raise PermissionDenied("No tienes permisos para editar esta ejecución.")

        execution.edited_output = ""
        execution.edited_at = None
        execution.edited_by = None
        execution.save(update_fields=["edited_output", "edited_at", "edited_by"])
        return Response(SkillExecutionSerializer(execution).data)
