from __future__ import annotations

import os
from typing import List

from rest_framework import serializers

from apps.chat.models import ChatSession, ChatMessage
from apps.document.services import accessible_documents_for
from apps.document.api.serializers import DocumentChunkSerializer
from apps.document.models import Document, SmartChunk

CHAT_MAX_DOCUMENTS_PER_SESSION = int(os.environ.get("CHAT_MAX_DOCUMENTS_PER_SESSION", "20"))


class ChatSessionSerializer(serializers.ModelSerializer):
    document_slugs = serializers.SerializerMethodField()
    primary_document_slug = serializers.SlugField(
        source="primary_document.slug",
        read_only=True,
        allow_null=True,
    )
    project_slug = serializers.SlugField(
        source="project.slug",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = ChatSession
        fields = (
            "id",
            "session_type",
            "title",
            "system_prompt",
            "model",
            "temperature",
            "language",
            "is_active",
            "created_at",
            "updated_at",
            "document_slugs",
            "primary_document_slug",
            "project_slug",
        )

    def get_document_slugs(self, obj: ChatSession) -> List[str]:
        return list(obj.allowed_documents.values_list("slug", flat=True))


class ChatSessionCreateSerializer(serializers.ModelSerializer):
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        allow_empty=True,
        write_only=True,
        required=False,
    )

    class Meta:
        model = ChatSession
        fields = (
            "title",
            "system_prompt",
            "model",
            "temperature",
            "language",
            "document_slugs",
        )

    def validate_document_slugs(self, slugs: List[str]) -> List[str]:
        # Si la lista está vacía, es válido (sesión sin documentos)
        if not slugs:
            return slugs

        unique_slugs = list(dict.fromkeys(slugs))
        if len(unique_slugs) > CHAT_MAX_DOCUMENTS_PER_SESSION:
            raise serializers.ValidationError(
                "Demasiados documentos seleccionados para una sesión de chat: "
                f"{len(unique_slugs)} seleccionados, máximo "
                f"{CHAT_MAX_DOCUMENTS_PER_SESSION}. "
                "Reducí el alcance para mantener respuestas completas y predecibles."
            )
        
        user = self.context["request"].user
        available_docs = accessible_documents_for(user, unique_slugs)
        found_slugs = set(available_docs.values_list("slug", flat=True))
        missing = [slug for slug in unique_slugs if slug not in found_slugs]
        if missing:
            raise serializers.ValidationError(
                f"Documentos no encontrados o sin permisos: {', '.join(missing)}"
            )
        return unique_slugs

    def create(self, validated_data):
        from apps.document.utils.llm import ROLE_BALANCED, resolve_model

        slugs = validated_data.pop("document_slugs", [])
        # Phase 2: new sessions default to the configured "balanced" model tier
        # (Sonnet under LLM_PROVIDER=anthropic; MODEL_COMPLETION otherwise).
        # Done here — not via the model field default — so the field stays
        # migration-stable regardless of the provider env.
        validated_data.setdefault("model", resolve_model(ROLE_BALANCED))
        session = ChatSession.objects.create(**validated_data)
        if slugs:
            docs = Document.objects.filter(slug__in=slugs)
            session.allowed_documents.set(docs)
        return session


def prefetch_chunks_by_id(chunk_ids: list[int], *, include_content: bool = True) -> dict[int, SmartChunk]:
    """Load SmartChunk rows once for many messages (avoids N+1 on list)."""
    unique_ids = list(dict.fromkeys(chunk_ids))
    if not unique_ids:
        return {}

    qs = SmartChunk.objects.filter(id__in=unique_ids).select_related("document")
    if not include_content:
        qs = qs.only(
            "id",
            "chunk_index",
            "document_id",
            "document__slug",
            "document__name",
        )
    return {chunk.id: chunk for chunk in qs}


def serialize_message_chunks(
    obj: ChatMessage,
    *,
    context: dict,
    include_content: bool = True,
    chunks_by_id: dict[int, SmartChunk] | None = None,
) -> list[dict]:
    if not obj.chunk_ids:
        return []

    resolved = chunks_by_id if chunks_by_id is not None else context.get("chunks_by_id")
    if resolved is None:
        resolved = prefetch_chunks_by_id(obj.chunk_ids, include_content=include_content)

    ordered_chunks = [
        resolved[chunk_id]
        for chunk_id in obj.chunk_ids
        if chunk_id in resolved
    ]
    if not ordered_chunks:
        return []

    if not include_content:
        return [
            {
                "id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "document_slug": chunk.document.slug,
                "document_name": chunk.document.name,
                "content": "",
            }
            for chunk in ordered_chunks
        ]

    serialized_by_id = {
        item["id"]: item
        for item in DocumentChunkSerializer(
            ordered_chunks,
            many=True,
            context=context,
        ).data
    }
    return [
        serialized_by_id[chunk_id]
        for chunk_id in obj.chunk_ids
        if chunk_id in serialized_by_id
    ]


class ChatMessageSerializer(serializers.ModelSerializer):
    chunks = serializers.SerializerMethodField()

    class Meta:
        model = ChatMessage
        fields = (
            "id",
            "session",
            "role",
            "content",
            "chunk_ids",
            "chunks",
            "metadata",
            "created_at",
        )
        read_only_fields = fields

    def get_chunks(self, obj: ChatMessage):
        include_content = self.context.get("include_chunk_content", True)
        return serialize_message_chunks(
            obj,
            context=self.context,
            include_content=include_content,
        )


class ChatMessageCreateSerializer(serializers.Serializer):
    session = serializers.PrimaryKeyRelatedField(queryset=ChatSession.objects.all())
    content = serializers.CharField(allow_blank=False, max_length=4000)
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        allow_empty=True,
        required=False,
    )
    response_mode = serializers.ChoiceField(
        choices=("puntual", "panorama", "comparacion", "extraccion", "tabla"),
        required=False,
        allow_null=True,
    )

    def validate_session(self, session: ChatSession):
        user = self.context["request"].user
        from apps.user.access import within_institution

        if not user.is_staff and (
            session.owner_id != user.id
            or (session.project_id and not within_institution(user, session.project.organization_id))
        ):
            raise serializers.ValidationError("No tienes acceso a esta sesión.")
        if not session.is_active:
            raise serializers.ValidationError("La sesión está inactiva.")
        doc_count = session.allowed_documents.count()
        if doc_count > CHAT_MAX_DOCUMENTS_PER_SESSION:
            raise serializers.ValidationError(
                "Esta sesión tiene demasiados documentos para un chat predecible: "
                f"{doc_count} documentos, máximo {CHAT_MAX_DOCUMENTS_PER_SESSION}. "
                "Creá una sesión con menos documentos o aumentá "
                "CHAT_MAX_DOCUMENTS_PER_SESSION si el entorno lo soporta."
            )
        return session

    def validate_document_slugs(self, slugs: List[str]) -> List[str]:
        unique_slugs = list(dict.fromkeys(slugs))
        if len(unique_slugs) > CHAT_MAX_DOCUMENTS_PER_SESSION:
            raise serializers.ValidationError(
                "Demasiados documentos seleccionados para una consulta: "
                f"{len(unique_slugs)} seleccionados, máximo "
                f"{CHAT_MAX_DOCUMENTS_PER_SESSION}."
            )
        if not unique_slugs:
            return unique_slugs
        user = self.context["request"].user
        available_docs = accessible_documents_for(user, unique_slugs)
        found_slugs = set(available_docs.values_list("slug", flat=True))
        missing = [slug for slug in unique_slugs if slug not in found_slugs]
        if missing:
            raise serializers.ValidationError(
                f"Documentos no encontrados o sin permisos: {', '.join(missing)}"
            )
        return unique_slugs

