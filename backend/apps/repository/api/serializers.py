from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

from apps.user.sharing import resolve_share_target

from apps.document.models import Document
from apps.document.services import accessible_documents_for
from apps.repository.models import (
    Repository,
    RepositoryDocument,
    RepositoryShare,
    RepositoryShareRole,
    RepositoryType,
)
from apps.skill.models import Skill

User = get_user_model()


class RepositoryDocumentSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(source="document.slug", read_only=True)
    name = serializers.CharField(source="document.name", read_only=True)
    category = serializers.CharField(source="document.category", read_only=True, allow_null=True)
    description = serializers.CharField(source="document.description", read_only=True)

    class Meta:
        model = RepositoryDocument
        fields = ("id", "slug", "name", "category", "description", "is_active", "added_at")
        read_only_fields = fields


class RepositorySerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True, allow_null=True)
    documents = RepositoryDocumentSerializer(
        source="repository_documents", many=True, read_only=True
    )
    document_count = serializers.SerializerMethodField()
    enabled_skill_slugs = serializers.SlugRelatedField(
        source="enabled_skills",
        many=True,
        read_only=True,
        slug_field="slug",
    )
    can_edit = serializers.SerializerMethodField()
    can_manage_shares = serializers.SerializerMethodField()

    class Meta:
        model = Repository
        fields = (
            "id",
            "slug",
            "name",
            "description",
            "repo_type",
            "category",
            "owner",
            "owner_email",
            "documents",
            "document_count",
            "enabled_skill_slugs",
            "can_edit",
            "can_manage_shares",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "slug",
            "repo_type",
            "owner",
            "owner_email",
            "documents",
            "document_count",
            "enabled_skill_slugs",
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

    def get_document_count(self, obj) -> int:
        return obj.repository_documents.count()


class RepositoryWriteSerializer(serializers.ModelSerializer):
    enabled_skill_slugs = serializers.ListField(
        child=serializers.SlugField(),
        required=False,
        allow_empty=True,
        write_only=True,
    )

    class Meta:
        model = Repository
        fields = ("name", "description", "enabled_skill_slugs")

    def validate_enabled_skill_slugs(self, slugs):
        request = self.context["request"]
        allowed = Skill.objects.filter(
            Q(owner__isnull=True) | Q(owner=request.user),
            Q(allowed_contexts__contains=["repository"]) | Q(allowed_contexts__contains=["any"]),
            slug__in=slugs,
        )
        found = set(allowed.values_list("slug", flat=True))
        missing = [slug for slug in slugs if slug not in found]
        if missing:
            raise serializers.ValidationError(
                f"Skills no encontradas o no disponibles para repositorios: {', '.join(missing)}"
            )
        self.context["validated_enabled_skills"] = list(allowed)
        return slugs

    def create(self, validated_data):
        validated_data.pop("enabled_skill_slugs", None)
        validated_data["repo_type"] = RepositoryType.PRIVATE
        repo = Repository.objects.create(**validated_data)
        repo.enabled_skills.set(self.context.get("validated_enabled_skills", []))
        return repo

    def update(self, instance, validated_data):
        should_sync_skills = "enabled_skill_slugs" in validated_data
        validated_data.pop("enabled_skill_slugs", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if should_sync_skills:
            instance.enabled_skills.set(self.context.get("validated_enabled_skills", []))
        return instance


class RepositoryDocumentAttachSerializer(serializers.Serializer):
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        allow_empty=False,
    )

    def validate_document_slugs(self, slugs):
        request = self.context["request"]
        docs = accessible_documents_for(request.user, slugs)
        found_slugs = set(docs.values_list("slug", flat=True))
        missing = [slug for slug in slugs if slug not in found_slugs]
        if missing:
            raise serializers.ValidationError(
                f"Documentos no encontrados o sin permisos: {', '.join(missing)}"
            )
        self.context["validated_documents"] = list(docs)
        return slugs

    def get_documents(self):
        return self.context.get("validated_documents", [])


class RepositoryShareSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = RepositoryShare
        fields = ("id", "user", "user_email", "role", "created_at")
        read_only_fields = ("id", "user_email", "created_at")


class RepositoryShareRoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=RepositoryShareRole.choices)


class RepositoryShareWriteSerializer(serializers.Serializer):
    user_email = serializers.EmailField()
    role = serializers.ChoiceField(choices=RepositoryShareRole.choices)

    def validate(self, attrs):
        email = attrs.get("user_email")
        user = resolve_share_target(self.context["request"].user, email)

        repository = self.context.get("repository")
        if repository and repository.owner_id == user.id:
            raise serializers.ValidationError({
                "user_email": "No puedes compartir el repositorio contigo mismo."
            })

        attrs["user"] = user
        return attrs
