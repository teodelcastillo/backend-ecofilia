from __future__ import annotations

from django.db.models import Count, Prefetch
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.chat.models import ChatSession
from apps.chat.api.serializers import ChatSessionSerializer, ChatSessionCreateSerializer
from apps.document.models import Document
from apps.repository.api.permissions import RepositoryAccessPermission
from apps.repository.api.serializers import (
    RepositoryDocumentAttachSerializer,
    RepositorySerializer,
    RepositoryShareRoleUpdateSerializer,
    RepositoryShareSerializer,
    RepositoryShareWriteSerializer,
    RepositoryWriteSerializer,
)
from apps.repository.models import Repository, RepositoryDocument, RepositoryShare


class RepositoryViewSet(viewsets.ModelViewSet):
    queryset = Repository.objects.none()
    permission_classes = [IsAuthenticated, RepositoryAccessPermission]
    serializer_class = RepositorySerializer
    lookup_field = "slug"

    def get_queryset(self):
        user = self.request.user
        return (
            Repository.objects.for_user(user)
            .prefetch_related(
                Prefetch(
                    "repository_documents",
                    queryset=RepositoryDocument.objects.select_related("document"),
                ),
                Prefetch(
                    "shares",
                    queryset=RepositoryShare.objects.select_related("user"),
                ),
                "enabled_skills",
            )
        )

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return RepositoryWriteSerializer
        return RepositorySerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def create(self, request, *args, **kwargs):
        """
        Return full repository payload (including slug) after create.
        Frontend links documents immediately using that slug.
        """
        write_serializer = self.get_serializer(data=request.data)
        write_serializer.is_valid(raise_exception=True)
        self.perform_create(write_serializer)
        repo = write_serializer.instance
        read_serializer = RepositorySerializer(repo, context=self.get_serializer_context())
        headers = self.get_success_headers(read_serializer.data)
        return Response(read_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_update(self, serializer):
        repo = self.get_object()
        if not repo.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para editar este repositorio.")
        serializer.save()

    def perform_destroy(self, instance):
        if not instance.can_edit(self.request.user):
            raise PermissionDenied("No tienes permisos para eliminar este repositorio.")
        instance.delete()

    # ------------------------------------------------------------------ #
    # Documents                                                            #
    # ------------------------------------------------------------------ #

    @action(detail=True, methods=["post"], url_path="documents", url_name="add-documents")
    def add_documents(self, request, slug=None):
        repo = self.get_object()
        if not repo.can_edit(request.user):
            raise PermissionDenied("No tienes permisos para agregar fuentes a este repositorio.")
        serializer = RepositoryDocumentAttachSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        for document in serializer.get_documents():
            RepositoryDocument.objects.get_or_create(
                repository=repo, document=document
            )
        return Response(self._serialize_repo(repo), status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"documents/(?P<document_slug>[^/]+)",
        url_name="remove-document",
    )
    def remove_document(self, request, slug=None, document_slug=None):
        repo = self.get_object()
        if not repo.can_edit(request.user):
            raise PermissionDenied("No tienes permisos para modificar este repositorio.")
        document = get_object_or_404(Document, slug=document_slug)
        deleted, _ = RepositoryDocument.objects.filter(
            repository=repo, document=document
        ).delete()
        if not deleted:
            return Response(
                {"detail": "Documento no encontrado en el repositorio."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(
        detail=True,
        methods=["patch"],
        url_path=r"documents/(?P<document_slug>[^/]+)/toggle",
        url_name="toggle-document",
    )
    def toggle_document(self, request, slug=None, document_slug=None):
        """Toggle is_active for a document in this repository."""
        repo = self.get_object()
        if not repo.can_manage_sources(request.user):
            raise PermissionDenied("No tienes permisos para gestionar las fuentes de este repositorio.")
        document = get_object_or_404(Document, slug=document_slug)
        repo_doc = get_object_or_404(RepositoryDocument, repository=repo, document=document)
        repo_doc.is_active = not repo_doc.is_active
        repo_doc.save(update_fields=["is_active"])
        return Response({"slug": document_slug, "is_active": repo_doc.is_active})

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="shares",
        url_name="shares",
    )
    def shares(self, request, slug=None):
        repo = self.get_object()
        self._ensure_share_manager(repo)
        if request.method == "GET":
            serializer = RepositoryShareSerializer(
                repo.shares.select_related("user"),
                many=True,
            )
            return Response(serializer.data)

        serializer = RepositoryShareWriteSerializer(
            data=request.data,
            context={"repository": repo, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        share, _ = RepositoryShare.objects.update_or_create(
            repository=repo,
            user=serializer.validated_data["user"],
            defaults={"role": serializer.validated_data["role"]},
        )
        output = RepositoryShareSerializer(share)
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"shares/(?P<share_id>[^/]+)",
        url_name="share-detail",
    )
    def manage_share(self, request, slug=None, share_id=None):
        repo = self.get_object()
        self._ensure_share_manager(repo)
        share = get_object_or_404(RepositoryShare, repository=repo, pk=share_id)

        if request.method == "PATCH":
            serializer = RepositoryShareRoleUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            share.role = serializer.validated_data["role"]
            share.save(update_fields=["role"])
            return Response(RepositoryShareSerializer(share).data)

        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------ #
    # Chat sessions                                                        #
    # ------------------------------------------------------------------ #

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="chat-sessions",
        url_name="chat-sessions",
    )
    def chat_sessions(self, request, slug=None):
        repo = self.get_object()

        if request.method == "GET":
            qs = (
                ChatSession.objects.filter(owner=request.user, repository=repo)
                .annotate(_ecofilia_msg_count=Count("messages"))
                .filter(_ecofilia_msg_count__gt=0)
                .prefetch_related("allowed_documents")
                .order_by("-updated_at")
            )
            serializer = ChatSessionSerializer(qs, many=True, context={"request": request})
            return Response(serializer.data)

        create_serializer = ChatSessionCreateSerializer(
            data=request.data, context={"request": request}
        )
        create_serializer.is_valid(raise_exception=True)
        validated = create_serializer.validated_data
        force_new = str(request.data.get("force_new", "")).lower() in {
            "1",
            "true",
            "yes",
        }

        # By default, resume the latest active repository chat session so users
        # keep conversational continuity across workspace visits.
        if not force_new:
            existing_session = (
                ChatSession.objects.filter(
                    owner=request.user,
                    repository=repo,
                    is_active=True,
                )
                .prefetch_related("allowed_documents")
                .order_by("-updated_at")
                .first()
            )
            if existing_session is not None:
                output = ChatSessionSerializer(
                    existing_session, context={"request": request}
                )
                return Response(output.data, status=status.HTTP_200_OK)

        session = ChatSession.objects.create(
            owner=request.user,
            repository=repo,
            title=validated.get("title", f"Repository: {repo.name}"),
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

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _serialize_repo(self, repo: Repository):
        refreshed = self.get_queryset().get(pk=repo.pk)
        return RepositorySerializer(refreshed, context=self.get_serializer_context()).data

    def _ensure_share_manager(self, repo: Repository):
        if not repo.can_manage_shares(self.request.user):
            raise PermissionDenied("No puedes administrar los permisos de este repositorio.")
