from __future__ import annotations

import logging

from django.db.models import Count, OuterRef, Prefetch, Q, Subquery  # Count used in subquery helper
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)

from apps.chat.models import ChatMessage, ChatSession, ChatSessionType, MessageRole
from apps.chat.api.serializers import (
    ChatMessageSerializer,
    ChatSessionSerializer,
    ChatSessionCreateSerializer,
)
from apps.chat.services.copilot import (
    generate_copilot_autocomplete,
    initialize_project_structure,
    process_copilot_message,
)
from apps.document.models import Document
from apps.evaluation.api.serializers import EvaluationRunSerializer
from apps.evaluation.models import EvaluationRun, PillarEvaluationResult
from apps.project.api.permissions import ProjectAccessPermission
from apps.project.api.serializers import (
    AiFillRequestSerializer,
    CopilotAutocompleteSerializer,
    CopilotMessageCreateSerializer,
    InitializeStructureSerializer,
    ProjectDeliverableCreateSerializer,
    ProjectDeliverableSerializer,
    ProjectDeliverableUpdateSerializer,
    ProjectDocumentAttachSerializer,
    ProjectSectionCreateSerializer,
    ProjectSectionSerializer,
    ProjectSectionUpdateSerializer,
    ProjectSerializer,
    ProjectShareRoleUpdateSerializer,
    ProjectShareSerializer,
    ProjectShareWriteSerializer,
    ProjectWriteSerializer,
)
from apps.project.models import (
    Project,
    ProjectDeliverable,
    ProjectDocument,
    ProjectSection,
    ProjectShare,
    ProjectStructureTemplate,
)


def _skill_execution_count_subquery():
    """
    Returns a Subquery that counts SkillExecutions for a given project.
    Defined as a function to keep the import lazy and avoid circular dependencies.
    """
    from apps.skill.models import SkillExecution
    return (
        SkillExecution.objects
        .filter(project=OuterRef("pk"))
        .values("project")
        .annotate(c=Count("id"))
        .values("c")[:1]
    )


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.none()
    permission_classes = [IsAuthenticated, ProjectAccessPermission]
    serializer_class = ProjectSerializer
    lookup_field = "slug"

    def get_queryset(self):
        user = self.request.user
        return (
            Project.objects.for_user(user)
            .select_related("owner", "blueprint_document", "structure_template")
            .prefetch_related(
                Prefetch(
                    "project_documents",
                    queryset=ProjectDocument.objects.select_related("document"),
                ),
                Prefetch(
                    "shares",
                    queryset=ProjectShare.objects.select_related("user"),
                ),
                "enabled_skills",
            )
            .annotate(
                skill_executions_count=Subquery(
                    _skill_execution_count_subquery()
                )
            )
            .distinct()
        )

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return ProjectWriteSerializer
        return ProjectSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        project = self.get_object()
        if not project.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para editar este proyecto.")
        serializer.save()

    def perform_destroy(self, instance):
        if not instance.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para eliminar este proyecto.")
        instance.delete()

    @action(
        detail=True,
        methods=["post"],
        url_path="documents",
        url_name="add-document",
    )
    def add_documents(self, request, slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        serializer = ProjectDocumentAttachSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        for document in serializer.get_documents():
            ProjectDocument.objects.get_or_create(
                project=project,
                document=document,
                defaults={"added_by": request.user},
            )
        return Response(
            self._serialize_project(project),
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"documents/(?P<document_slug>[^/]+)",
        url_name="remove-document",
    )
    def remove_document(self, request, slug=None, document_slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        document = get_object_or_404(Document, slug=document_slug)
        deleted, _ = ProjectDocument.objects.filter(
            project=project, document=document
        ).delete()
        if not deleted:
            return Response(
                {"detail": "Documento no encontrado en el proyecto."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="shares",
        url_name="shares",
    )
    def shares(self, request, slug=None):
        project = self.get_object()
        self._ensure_share_manager(project)
        if request.method == "GET":
            serializer = ProjectShareSerializer(
                project.shares.select_related("user"),
                many=True,
            )
            return Response(serializer.data)

        serializer = ProjectShareWriteSerializer(
            data=request.data,
            context={"project": project},
        )
        serializer.is_valid(raise_exception=True)
        share, _ = ProjectShare.objects.update_or_create(
            project=project,
            user=serializer.validated_data["user"],
            defaults={"role": serializer.validated_data["role"]},
        )
        output = ProjectShareSerializer(share)
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"shares/(?P<share_id>[^/]+)",
        url_name="share-detail",
    )
    def manage_share(self, request, slug=None, share_id=None):
        project = self.get_object()
        self._ensure_share_manager(project)
        share = get_object_or_404(ProjectShare, project=project, pk=share_id)

        if request.method == "PATCH":
            serializer = ProjectShareRoleUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            share.role = serializer.validated_data["role"]
            share.save(update_fields=["role"])
            return Response(ProjectShareSerializer(share).data)

        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["get"],
        url_path="skill-executions",
        url_name="skill-executions",
    )
    def skill_executions(self, request, slug=None):
        """List skill executions for this project (shared collaborators see all runs)."""
        project = self.get_object()
        if not project.can_view(request.user):
            raise PermissionDenied("No tienes permisos para ver este proyecto.")
        from apps.skill.models import SkillExecution
        from apps.skill.api.serializers import SkillExecutionSerializer
        qs = (
            SkillExecution.objects.filter(project=project)
            .select_related("skill", "owner", "document")
        )
        executions = qs.order_by("-created_at")
        serializer = SkillExecutionSerializer(executions, many=True)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["get"],
        url_path="evaluation-runs",
        url_name="evaluation-runs",
    )
    def evaluation_runs(self, request, slug=None):
        """Runs de evaluación en este proyecto (colaboradores con acceso ven todos)."""
        project = self.get_object()

        if not project.can_view(request.user):
            raise PermissionDenied("No tienes permisos para ver este proyecto.")

        qs = (
            EvaluationRun.objects.filter(project=project)
            .select_related("evaluation", "owner", "project")
            .prefetch_related(
                Prefetch(
                    "pillar_results",
                    queryset=PillarEvaluationResult.objects.prefetch_related("metric_results"),
                )
            )
        )
        runs = qs.order_by("-created_at")

        serializer = EvaluationRunSerializer(runs, many=True)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="chat-sessions",
        url_name="chat-sessions",
    )
    def chat_sessions(self, request, slug=None):
        project = self.get_object()

        if request.method == "GET":
            qs = (
                ChatSession.objects.filter(owner=request.user, project=project)
                .annotate(_ecofilia_msg_count=Count("messages"))
                .filter(_ecofilia_msg_count__gt=0)
                .prefetch_related("allowed_documents")
                .order_by("-updated_at")
            )
            serializer = ChatSessionSerializer(
                qs, many=True, context={"request": request},
            )
            return Response(serializer.data)

        create_serializer = ChatSessionCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        create_serializer.is_valid(raise_exception=True)
        validated = create_serializer.validated_data

        session = ChatSession.objects.create(
            owner=request.user,
            project=project,
            title=validated.get("title", f"Chat: {project.name}"),
            system_prompt=validated.get("system_prompt", ""),
            model=validated.get("model", ChatSession._meta.get_field("model").default),
            temperature=validated.get("temperature", 0.1),
            language=validated.get("language", "es"),
        )

        slugs = validated.get("document_slugs", [])
        if slugs:
            docs = Document.objects.filter(slug__in=slugs)
            session.allowed_documents.set(docs)

        output = ChatSessionSerializer(session, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------
    # Deliverable endpoints — projects now hold N independent deliverables.
    # The legacy ``/structure`` endpoint below redirects to the primary
    # deliverable so older clients keep working during the transition.
    # ------------------------------------------------------------------

    def _resolve_deliverable(self, project: Project, deliv_slug: str | None):
        """Look up a deliverable by slug, or the project's primary one."""
        qs = ProjectDeliverable.objects.filter(project=project)
        if deliv_slug:
            return get_object_or_404(qs, slug=deliv_slug)
        primary = qs.filter(is_primary=True).first()
        if primary is None:
            # Defensive: any existing deliverable beats failing the request.
            primary = qs.order_by("position", "created_at").first()
        if primary is None:
            primary = ProjectDeliverable.objects.create(
                project=project,
                name="Entregable principal",
                template=project.structure_template,
                is_primary=True,
                position=1,
            )
        return primary

    @action(
        detail=True, methods=["get", "post"],
        url_path="deliverables", url_name="deliverables",
    )
    def deliverables(self, request, slug=None):
        project = self.get_object()
        if request.method == "GET":
            qs = ProjectDeliverable.objects.filter(project=project).select_related("template")
            return Response(
                ProjectDeliverableSerializer(qs, many=True).data,
            )

        self._ensure_editor(project)
        serializer = ProjectDeliverableCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        template = None
        tpl_slug = data.get("template_slug") or None
        if tpl_slug:
            try:
                template = ProjectStructureTemplate.objects.get(slug=tpl_slug)
            except ProjectStructureTemplate.DoesNotExist:
                return Response(
                    {"detail": f"Template '{tpl_slug}' no encontrado."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        next_pos = (
            ProjectDeliverable.objects.filter(project=project)
            .order_by("-position").values_list("position", flat=True).first()
        )
        deliverable = ProjectDeliverable.objects.create(
            project=project,
            name=data["name"],
            template=template,
            is_primary=False,
            position=(next_pos or 0) + 1,
            status=data.get("status") or "draft",
        )
        if template is not None:
            try:
                initialize_project_structure(
                    project, template.slug, deliverable=deliverable,
                )
            except Exception as exc:  # pragma: no cover - defensive
                deliverable.delete()
                return Response(
                    {"detail": f"No se pudo inicializar la estructura: {exc}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(
            ProjectDeliverableSerializer(deliverable).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True, methods=["patch", "delete"],
        url_path=r"deliverables/(?P<deliv_slug>[^/]+)",
        url_name="deliverable-detail",
    )
    def deliverable_detail(self, request, slug=None, deliv_slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, deliv_slug)

        if request.method == "DELETE":
            if deliverable.is_primary:
                # Allow deleting the primary only when there are other deliverables;
                # we promote one of them to primary.
                next_primary = (
                    ProjectDeliverable.objects.filter(project=project)
                    .exclude(pk=deliverable.pk)
                    .order_by("position", "created_at")
                    .first()
                )
                if next_primary is None:
                    return Response(
                        {"detail": "No se puede eliminar el único entregable del proyecto."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                next_primary.is_primary = True
                next_primary.save(update_fields=["is_primary", "updated_at"])
            deliverable.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ProjectDeliverableUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        update_fields = []
        for field in ("name", "status", "position"):
            if field in data:
                setattr(deliverable, field, data[field])
                update_fields.append(field)
        if "is_primary" in data and data["is_primary"]:
            # Promoting another to primary demotes the current primary.
            ProjectDeliverable.objects.filter(
                project=project, is_primary=True,
            ).exclude(pk=deliverable.pk).update(is_primary=False)
            deliverable.is_primary = True
            update_fields.append("is_primary")
        if update_fields:
            update_fields.append("updated_at")
            deliverable.save(update_fields=update_fields)
        return Response(ProjectDeliverableSerializer(deliverable).data)

    # ------------------------------------------------------------------
    # Structure endpoints — scoped to a deliverable.
    # ------------------------------------------------------------------

    def _structure_payload(self, deliverable):
        sections = ProjectSection.objects.filter(deliverable=deliverable).order_by("position")
        return {
            "deliverable_slug": deliverable.slug,
            "deliverable_name": deliverable.name,
            "template_slug": deliverable.template.slug if deliverable.template else None,
            "template_name": deliverable.template.name if deliverable.template else None,
            "sections": ProjectSectionSerializer(sections, many=True).data,
        }

    @action(detail=True, methods=["get"], url_path="structure", url_name="structure")
    def structure(self, request, slug=None):
        """Legacy structure endpoint — returns the primary deliverable."""
        project = self.get_object()
        deliverable = self._resolve_deliverable(project, None)
        return Response(self._structure_payload(deliverable))

    @action(
        detail=True, methods=["get"],
        url_path=r"deliverables/(?P<deliv_slug>[^/]+)/structure",
        url_name="deliverable-structure",
    )
    def deliverable_structure(self, request, slug=None, deliv_slug=None):
        project = self.get_object()
        deliverable = self._resolve_deliverable(project, deliv_slug)
        return Response(self._structure_payload(deliverable))

    @action(detail=True, methods=["put"], url_path="structure/initialize", url_name="structure-initialize")
    def structure_initialize(self, request, slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, None)
        return self._initialize_structure_for_deliverable(project, deliverable, request)

    @action(
        detail=True, methods=["put"],
        url_path=r"deliverables/(?P<deliv_slug>[^/]+)/structure/initialize",
        url_name="deliverable-structure-initialize",
    )
    def deliverable_structure_initialize(self, request, slug=None, deliv_slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, deliv_slug)
        return self._initialize_structure_for_deliverable(project, deliverable, request)

    def _initialize_structure_for_deliverable(self, project, deliverable, request):
        serializer = InitializeStructureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            sections = initialize_project_structure(
                project,
                serializer.validated_data["template_slug"],
                deliverable=deliverable,
            )
        except Exception as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            ProjectSectionSerializer(sections, many=True).data,
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True, methods=["patch", "delete"],
        url_path=r"structure/sections/(?P<position>\d+)",
        url_name="structure-section-update",
    )
    def update_section(self, request, slug=None, position=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, None)
        return self._update_section_for_deliverable(deliverable, int(position), request)

    @action(
        detail=True, methods=["patch", "delete"],
        url_path=r"deliverables/(?P<deliv_slug>[^/]+)/sections/(?P<position>\d+)",
        url_name="deliverable-section-update",
    )
    def deliverable_update_section(self, request, slug=None, deliv_slug=None, position=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, deliv_slug)
        return self._update_section_for_deliverable(deliverable, int(position), request)

    def _update_section_for_deliverable(self, deliverable, position: int, request):
        section = get_object_or_404(
            ProjectSection, deliverable=deliverable, position=position,
        )

        if request.method == "DELETE":
            removed_position = section.position
            section.delete()
            following = ProjectSection.objects.filter(
                deliverable=deliverable, position__gt=removed_position,
            ).order_by("position")
            for sec in following:
                sec.position -= 1
                sec.save(update_fields=["position"])
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ProjectSectionUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        new_position = data.get("position")
        if new_position is not None and new_position != section.position:
            current = section.position
            section.position = (
                ProjectSection.objects.filter(deliverable=deliverable).count()
                + max(current, new_position)
                + 1
            )
            section.save(update_fields=["position"])
            if new_position < current:
                shifted = ProjectSection.objects.filter(
                    deliverable=deliverable,
                    position__gte=new_position,
                    position__lt=current,
                ).order_by("-position")
                for sec in shifted:
                    sec.position += 1
                    sec.save(update_fields=["position"])
            else:
                shifted = ProjectSection.objects.filter(
                    deliverable=deliverable,
                    position__gt=current,
                    position__lte=new_position,
                ).order_by("position")
                for sec in shifted:
                    sec.position -= 1
                    sec.save(update_fields=["position"])
            section.position = new_position
            section.save(update_fields=["position"])

        update_fields = []
        for field in ("title", "description", "status", "notes", "output_snapshot"):
            if field in data:
                setattr(section, field, data[field])
                update_fields.append(field)
        if update_fields:
            section.save(update_fields=update_fields)
        return Response(ProjectSectionSerializer(section).data)

    @action(
        detail=True, methods=["post"],
        url_path="structure/sections", url_name="structure-section-create",
    )
    def create_section(self, request, slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, None)
        return self._create_section_for_deliverable(project, deliverable, request)

    @action(
        detail=True, methods=["post"],
        url_path=r"deliverables/(?P<deliv_slug>[^/]+)/sections",
        url_name="deliverable-section-create",
    )
    def deliverable_create_section(self, request, slug=None, deliv_slug=None):
        project = self.get_object()
        self._ensure_editor(project)
        deliverable = self._resolve_deliverable(project, deliv_slug)
        return self._create_section_for_deliverable(project, deliverable, request)

    def _create_section_for_deliverable(self, project, deliverable, request):
        serializer = ProjectSectionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        last_position = (
            ProjectSection.objects.filter(deliverable=deliverable)
            .order_by("-position").values_list("position", flat=True).first()
        )
        position = data.get("position") or ((last_position or 0) + 1)
        conflicting = list(
            ProjectSection.objects.filter(
                deliverable=deliverable, position__gte=position,
            ).order_by("-position")
        )
        for sec in conflicting:
            sec.position += 1
            sec.save(update_fields=["position"])
        section = ProjectSection.objects.create(
            project=project,
            deliverable=deliverable,
            title=data["title"],
            description=data.get("description", ""),
            position=position,
        )
        return Response(
            ProjectSectionSerializer(section).data,
            status=status.HTTP_201_CREATED,
        )

    # ------------------------------------------------------------------
    # Copilot endpoints
    # ------------------------------------------------------------------

    @action(
        detail=True, methods=["get", "post"],
        url_path="copilot/sessions", url_name="copilot-sessions",
    )
    def copilot_sessions(self, request, slug=None):
        project = self.get_object()
        # Copilot sessions live "inside" a deliverable: the redactor writes
        # one document at a time. The frontend may filter by
        # ``?deliverable=<slug>`` and pass ``deliverable_slug`` in the
        # creation body. When omitted, we default to the primary deliverable
        # so behaviour matches the single-deliverable era.
        deliv_slug = request.query_params.get("deliverable") or request.data.get(
            "deliverable_slug"
        )

        if request.method == "GET":
            qs = (
                ChatSession.objects.filter(
                    owner=request.user,
                    project=project,
                    session_type=ChatSessionType.COPILOT,
                )
                .prefetch_related("allowed_documents")
                .order_by("-updated_at")
            )
            if deliv_slug:
                deliverable = self._resolve_deliverable(project, deliv_slug)
                qs = qs.filter(deliverable=deliverable)
            serializer = ChatSessionSerializer(
                qs, many=True, context={"request": request},
            )
            return Response(serializer.data)

        deliverable = self._resolve_deliverable(project, deliv_slug)
        doc_ids = (
            ProjectDocument.objects.filter(project=project)
            .values_list("document_id", flat=True)
        )
        docs = Document.objects.filter(id__in=doc_ids)

        session = ChatSession.objects.create(
            owner=request.user,
            project=project,
            deliverable=deliverable,
            session_type=ChatSessionType.COPILOT,
            title=f"Copilot: {deliverable.name}",
            system_prompt="",
            model=ChatSession._meta.get_field("model").default,
            temperature=0.3,
            language="es",
        )
        session.allowed_documents.set(docs)

        output = ChatSessionSerializer(session, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=["post"],
        url_path="copilot/messages", url_name="copilot-messages",
    )
    def copilot_messages(self, request, slug=None):
        project = self.get_object()
        serializer = CopilotMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        content = serializer.validated_data["content"]

        session_id = request.data.get("session")
        if session_id:
            session = get_object_or_404(
                ChatSession,
                pk=session_id,
                owner=request.user,
                project=project,
                session_type=ChatSessionType.COPILOT,
            )
        else:
            session = (
                ChatSession.objects.filter(
                    owner=request.user,
                    project=project,
                    session_type=ChatSessionType.COPILOT,
                )
                .order_by("-updated_at")
                .first()
            )
            if session is None:
                return Response(
                    {"detail": "No copilot session found. Create one first."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        user_message = ChatMessage.objects.create(
            session=session, role=MessageRole.USER, content=content,
        )

        try:
            answer_text, metadata, chunk_ids = process_copilot_message(
                session, content, request.user,
            )
        except Exception as exc:
            user_message.delete()
            return Response(
                {"detail": f"Copilot error: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        assistant_message = ChatMessage.objects.create(
            session=session,
            role=MessageRole.ASSISTANT,
            content=answer_text,
            chunk_ids=chunk_ids,
            metadata=metadata,
        )

        return Response(
            {
                "user_message": ChatMessageSerializer(user_message).data,
                "assistant_message": ChatMessageSerializer(assistant_message).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True, methods=["post"],
        url_path="copilot/autocomplete", url_name="copilot-autocomplete",
    )
    def copilot_autocomplete(self, request, slug=None):
        """
        Inline ghost-text suggestion for the project editor.

        The editor calls this on caret pause or via keyboard shortcut. The
        response is a short continuation text the frontend overlays as ghost
        text — Tab to accept, Esc to dismiss.
        """
        project = self.get_object()
        serializer = CopilotAutocompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        section = None
        position = data.get("section_position")
        if position:
            section = ProjectSection.objects.filter(
                project=project, position=position,
            ).first()

        try:
            completion, usage = generate_copilot_autocomplete(
                project,
                before=data.get("before", ""),
                after=data.get("after", ""),
                section=section,
                doc_title=data.get("doc_title") or None,
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": f"Autocomplete error: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "completion": completion,
                "usage": usage,
            },
            status=status.HTTP_200_OK,
        )

    # ------------------------------------------------------------------
    # AI fill
    # ------------------------------------------------------------------

    @action(detail=False, methods=["get"], url_path="ai-fill-fields")
    def ai_fill_fields(self, request):
        """
        Return the list of fields that ``ai_fill`` can extract.

        The frontend uses this to build the field picker UI without
        hardcoding field keys or labels.
        """
        from apps.project.services.ai_fill import available_fields
        return Response({"fields": available_fields()})

    @action(detail=True, methods=["post"], url_path="ai-fill")
    def ai_fill(self, request, slug=None):
        """
        Extract structured fields from the project's blueprint document using AI.

        The extracted values are returned to the frontend for review — the user
        decides which ones to accept before they are saved to ``context_notes``
        via a standard PATCH on the project.  Nothing is persisted here.

        Request body::

            {"fields": ["objetivo", "componentes"]}

        Response::

            {"results": {"objetivo": "...", "componentes": "..."}}

        Requires editor permission on the project.  The project must have a
        blueprint document with extracted text.
        """
        project = self.get_object()
        self._ensure_editor(project)

        serializer = AiFillRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        field_keys = serializer.validated_data["fields"]

        from apps.project.services.ai_fill import ai_fill_project

        try:
            results = ai_fill_project(project, field_keys)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("ai_fill failed for project=%s", project.slug)
            return Response(
                {"detail": "No se pudo extraer la información del documento."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({"results": results})

    def _ensure_editor(self, project: Project):
        if not project.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para modificar este proyecto.")

    def _ensure_share_manager(self, project: Project):
        if not project.can_manage_shares(self.request.user):
            raise PermissionDenied("No puedes administrar los permisos de este proyecto.")

    def _serialize_project(self, project: Project):
        refreshed = self.get_queryset().get(pk=project.pk)
        return ProjectSerializer(
            refreshed, context=self.get_serializer_context()
        ).data


class StructureTemplateViewSet(
    viewsets.ModelViewSet,
):
    """CRUD viewset for project structure templates."""

    permission_classes = [IsAuthenticated]
    lookup_field = "slug"

    def get_queryset(self):
        from apps.project.models import ProjectStructureTemplate
        qs = ProjectStructureTemplate.objects.prefetch_related("sections").annotate(
            section_count=Count("sections"),
        )
        user = self.request.user
        if user.is_staff:
            return qs
        return qs.filter(Q(owner=user) | Q(owner__isnull=True))

    def get_serializer_class(self):
        from apps.project.api.serializers import (
            ProjectStructureTemplateListSerializer,
            ProjectStructureTemplateSerializer,
            ProjectStructureTemplateWriteSerializer,
        )

        if self.action == "list":
            return ProjectStructureTemplateListSerializer
        if self.action in {"create", "update", "partial_update"}:
            return ProjectStructureTemplateWriteSerializer
        return ProjectStructureTemplateSerializer

    def perform_create(self, serializer):
        is_global = bool(serializer.validated_data.get("is_global", False))
        if is_global:
            if not self.request.user.is_staff:
                raise PermissionDenied("Solo usuarios staff pueden crear plantillas globales.")
            serializer.save(owner=None)
            return
        serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        instance = self.get_object()
        is_global_target = bool(serializer.validated_data.get("is_global", instance.owner_id is None))
        if instance.owner_id is None:
            if not self.request.user.is_staff:
                raise PermissionDenied("Solo usuarios staff pueden editar plantillas globales.")
        else:
            if instance.owner_id != self.request.user.id and not self.request.user.is_staff:
                raise PermissionDenied("No puedes editar una plantilla que no te pertenece.")
        if is_global_target and not self.request.user.is_staff:
            raise PermissionDenied("Solo usuarios staff pueden convertir plantillas a globales.")
        if is_global_target:
            serializer.save(owner=None)
        elif instance.owner_id is None and not self.request.user.is_staff:
            raise PermissionDenied("Solo staff puede reasignar plantillas globales.")
        else:
            # Keep owner if already set; if global->local by staff, make owner current user.
            owner = instance.owner if instance.owner_id is not None else self.request.user
            serializer.save(owner=owner)

    def perform_destroy(self, instance):
        if instance.owner_id is None and not self.request.user.is_staff:
            raise PermissionDenied("Solo usuarios staff pueden eliminar plantillas globales.")
        if instance.owner_id is not None and instance.owner_id != self.request.user.id and not self.request.user.is_staff:
            raise PermissionDenied("No puedes eliminar una plantilla que no te pertenece.")
        instance.delete()

