from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

from apps.document.models import Document
from apps.document.services import accessible_documents_for
from apps.project.models import (
    Project,
    ProjectDeliverable,
    ProjectDeliverableStatus,
    ProjectDocument,
    ProjectSection,
    ProjectSectionStatus,
    ProjectShare,
    ProjectShareRole,
    ProjectStructureSection,
    ProjectStructureTemplate,
)
from apps.skill.models import Skill

User = get_user_model()


def _default_skills_for(user) -> list:
    """Skills a new project inherits from its owner's organization.

    Members of restricted orgs (CAF) have no UI to attach an agent to an
    operation, so a project created without an explicit skill list would be
    born with an empty workspace and no way out of it from the portal.
    """
    org = getattr(user, "organization", None)
    if org is None:
        return []
    return list(org.default_project_skills.all())


class ProjectDocumentSerializer(serializers.ModelSerializer):
    slug = serializers.CharField(source="document.slug", read_only=True)
    name = serializers.CharField(source="document.name", read_only=True)
    category = serializers.CharField(
        source="document.category", read_only=True
    )
    description = serializers.CharField(
        source="document.description", read_only=True
    )

    class Meta:
        model = ProjectDocument
        fields = (
            "id",
            "slug",
            "name",
            "category",
            "description",
            "is_primary",
            "note",
            "created_at",
        )
        read_only_fields = fields


class ProjectSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    owner_email = serializers.EmailField(
        source="owner.email", read_only=True
    )
    documents = ProjectDocumentSerializer(
        source="project_documents", many=True, read_only=True
    )
    skill_executions_count = serializers.IntegerField(read_only=True, default=0)
    enabled_skill_slugs = serializers.SlugRelatedField(
        source="enabled_skills",
        many=True,
        read_only=True,
        slug_field="slug",
    )
    blueprint_document_slug = serializers.SlugField(
        source="blueprint_document.slug",
        read_only=True,
        allow_null=True,
    )
    structure_template_slug = serializers.SlugField(
        source="structure_template.slug",
        read_only=True,
        allow_null=True,
    )
    can_edit = serializers.SerializerMethodField()
    can_manage_shares = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = (
            "id",
            "slug",
            "name",
            "description",
            "is_active",
            "owner",
            "owner_email",
            "documents",
            "skill_executions_count",
            "enabled_skill_slugs",
            "blueprint_document_slug",
            "context_notes",
            "copilot_enabled",
            "structure_template_slug",
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
            "documents",
            "skill_executions_count",
            "enabled_skill_slugs",
            "blueprint_document_slug",
            "structure_template_slug",
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


class ProjectWriteSerializer(ProjectSerializer):
    document_slugs = serializers.ListField(
        child=serializers.SlugField(),
        allow_empty=True,
        required=False,
        write_only=True,
    )
    enabled_skill_slugs = serializers.ListField(
        child=serializers.SlugField(),
        allow_empty=True,
        required=False,
        write_only=True,
    )
    blueprint_document_slug = serializers.SlugField(
        required=False,
        allow_null=True,
        allow_blank=False,
        write_only=True,
    )
    context_notes = serializers.JSONField(required=False)
    copilot_enabled = serializers.BooleanField(required=False)
    structure_template_slug = serializers.SlugField(
        required=False,
        allow_null=True,
        write_only=True,
    )

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ("document_slugs",)

    def validate_document_slugs(self, slugs):
        if not slugs:
            return []
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

    def validate_enabled_skill_slugs(self, slugs):
        request = self.context["request"]
        allowed = Skill.objects.filter(
            Q(owner__isnull=True) | Q(owner=request.user),
            Q(allowed_contexts__contains=["project"]) | Q(allowed_contexts__contains=["any"]),
            slug__in=slugs,
        )
        found_slugs = set(allowed.values_list("slug", flat=True))
        missing = [slug for slug in slugs if slug not in found_slugs]
        if missing:
            raise serializers.ValidationError(
                f"Skills no encontradas o no disponibles para proyectos: {', '.join(missing)}"
            )
        self.context["validated_enabled_skills"] = list(allowed)
        return slugs

    def validate(self, attrs):
        instance = self.instance
        has_blueprint = "blueprint_document_slug" in attrs
        if not has_blueprint:
            return attrs

        blueprint_slug = attrs.get("blueprint_document_slug")
        if blueprint_slug is None:
            return attrs

        if "document_slugs" in attrs:
            document_slugs = attrs.get("document_slugs") or []
        elif instance:
            document_slugs = list(
                instance.project_documents.values_list("document__slug", flat=True)
            )
        else:
            document_slugs = []

        if blueprint_slug not in document_slugs:
            raise serializers.ValidationError(
                {"blueprint_document_slug": "Blueprint document must be linked to the project."}
            )
        return attrs

    def create(self, validated_data):
        document_slugs = validated_data.pop("document_slugs", [])
        # Absent vs. explicitly empty are different intents: an omitted key
        # means "give me the org defaults", `[]` means "no agents".
        sent_skills = "enabled_skill_slugs" in validated_data
        validated_data.pop("enabled_skill_slugs", None)
        blueprint_slug = validated_data.pop("blueprint_document_slug", None)
        project = Project.objects.create(**validated_data)
        self._sync_documents(project, document_slugs)
        self._sync_blueprint(project, blueprint_slug)
        project.enabled_skills.set(
            self.context.get("validated_enabled_skills", [])
            if sent_skills
            else _default_skills_for(project.owner)
        )
        return project

    def update(self, instance, validated_data):
        document_slugs = validated_data.pop("document_slugs", None)
        should_sync_skills = "enabled_skill_slugs" in validated_data
        validated_data.pop("enabled_skill_slugs", None)
        should_sync_blueprint = "blueprint_document_slug" in validated_data
        blueprint_slug = validated_data.pop("blueprint_document_slug", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if document_slugs is not None:
            instance.project_documents.all().delete()
            self._sync_documents(instance, document_slugs)
        if should_sync_blueprint:
            self._sync_blueprint(instance, blueprint_slug)
        if should_sync_skills:
            instance.enabled_skills.set(self.context.get("validated_enabled_skills", []))
        return instance

    def _sync_documents(self, project: Project, slugs):
        if not slugs:
            return
        documents = self.context.get("validated_documents")
        if documents is None:
            documents = list(
                Document.objects.filter(slug__in=slugs)
            )
        existing_slugs = set(
            project.project_documents.values_list(
                "document__slug", flat=True
            )
        )
        for doc in documents:
            if doc.slug in existing_slugs:
                continue
            ProjectDocument.objects.create(
                project=project,
                document=doc,
                added_by=project.owner,
            )

    def _sync_blueprint(self, project: Project, blueprint_slug):
        if blueprint_slug is None:
            return
        doc = Document.objects.filter(slug=blueprint_slug).first()
        if not doc:
            return
        project.blueprint_document = doc
        project.save(update_fields=["blueprint_document"])
        # Keep the per-link is_primary flag in sync so the frontend can rely
        # on either field; otherwise stale is_primary rows would short-circuit
        # the doc-finder and show the old primary until a full refresh.
        project.project_documents.filter(is_primary=True).exclude(document=doc).update(is_primary=False)
        project.project_documents.filter(document=doc).update(is_primary=True)


class ProjectDocumentAttachSerializer(serializers.Serializer):
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


class ProjectShareSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = ProjectShare
        fields = ("id", "user", "user_email", "role", "created_at")
        read_only_fields = ("id", "user_email", "created_at")


class ProjectShareRoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=ProjectShareRole.choices)


class ProjectShareWriteSerializer(serializers.Serializer):
    user_email = serializers.EmailField()
    role = serializers.ChoiceField(choices=ProjectShareRole.choices)

    def validate(self, attrs):
        """Valida el email y obtiene el usuario"""
        email = attrs.get('user_email')
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise serializers.ValidationError({
                'user_email': f"No existe un usuario con el email: {email}"
            })
        
        project = self.context.get("project")
        if project and user == project.owner:
            raise serializers.ValidationError({
                'user_email': "No puedes compartir el proyecto contigo mismo."
            })
        
        # Reemplazar user_email con user para que la vista lo use
        attrs['user'] = user
        return attrs


# ---------------------------------------------------------------------------
# Structure template serializers
# ---------------------------------------------------------------------------

class ProjectStructureSectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectStructureSection
        fields = ("id", "title", "description", "position", "suggested_skill_slugs")
        read_only_fields = fields


class ProjectStructureTemplateSerializer(serializers.ModelSerializer):
    sections = ProjectStructureSectionSerializer(many=True, read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True, allow_null=True)
    is_global = serializers.SerializerMethodField()

    class Meta:
        model = ProjectStructureTemplate
        fields = (
            "id",
            "slug",
            "name",
            "description",
            "sections",
            "owner_email",
            "is_global",
            "created_at",
        )
        read_only_fields = fields

    def get_is_global(self, obj: ProjectStructureTemplate) -> bool:
        return obj.owner_id is None


class ProjectStructureTemplateListSerializer(serializers.ModelSerializer):
    section_count = serializers.IntegerField(read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True, allow_null=True)
    is_global = serializers.SerializerMethodField()

    class Meta:
        model = ProjectStructureTemplate
        fields = (
            "id",
            "slug",
            "name",
            "description",
            "section_count",
            "owner_email",
            "is_global",
            "created_at",
        )
        read_only_fields = fields

    def get_is_global(self, obj: ProjectStructureTemplate) -> bool:
        return obj.owner_id is None


class ProjectStructureSectionWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    position = serializers.IntegerField(min_value=1)
    suggested_skill_slugs = serializers.ListField(
        child=serializers.SlugField(),
        required=False,
        allow_empty=True,
    )


class ProjectStructureTemplateWriteSerializer(serializers.ModelSerializer):
    sections = ProjectStructureSectionWriteSerializer(many=True, required=False)
    slug = serializers.SlugField(required=False, allow_blank=True)

    is_global = serializers.BooleanField(required=False, write_only=True)

    class Meta:
        model = ProjectStructureTemplate
        fields = ("name", "slug", "description", "sections", "is_global")

    def validate(self, attrs):
        sections = attrs.get("sections")
        if sections is None:
            return attrs
        positions = [sec["position"] for sec in sections]
        if len(set(positions)) != len(positions):
            raise serializers.ValidationError(
                {"sections": "Each section position must be unique within a template."}
            )
        return attrs

    def validate_slug(self, value: str):
        from django.utils.text import slugify

        return slugify(value) if value else value

    def create(self, validated_data):
        from django.utils.text import slugify

        sections = validated_data.pop("sections", [])
        validated_data.pop("is_global", None)
        slug = validated_data.pop("slug", "")
        if not slug:
            slug = slugify(validated_data["name"])
        template = ProjectStructureTemplate.objects.create(slug=slug, **validated_data)
        self._replace_sections(template, sections)
        return template

    def update(self, instance, validated_data):
        sections = validated_data.pop("sections", None)
        validated_data.pop("is_global", None)
        slug = validated_data.pop("slug", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if slug is not None and slug != "":
            instance.slug = slug
        instance.save()
        if sections is not None:
            self._replace_sections(instance, sections)
        return instance

    def _replace_sections(self, template: ProjectStructureTemplate, sections: list[dict]):
        template.sections.all().delete()
        for sec in sorted(sections, key=lambda s: s["position"]):
            ProjectStructureSection.objects.create(
                template=template,
                title=sec["title"],
                description=sec.get("description", ""),
                position=sec["position"],
                suggested_skill_slugs=sec.get("suggested_skill_slugs", []),
            )


# ---------------------------------------------------------------------------
# Project section serializers
# ---------------------------------------------------------------------------

class ProjectSectionSerializer(serializers.ModelSerializer):
    # Inherited from the template (when the section was initialised from one).
    # Surfaces the curated skill shortcuts on the deliverable outline so the
    # consultant can act on a section without leaving it.
    suggested_skill_slugs = serializers.SerializerMethodField()
    deliverable_slug = serializers.CharField(
        source="deliverable.slug", read_only=True, default=None,
    )

    class Meta:
        model = ProjectSection
        fields = (
            "id", "title", "description", "position", "status",
            "notes", "output_snapshot", "suggested_skill_slugs",
            "deliverable_slug",
            "updated_at", "created_at",
        )
        read_only_fields = (
            "id", "position", "suggested_skill_slugs", "deliverable_slug",
            "updated_at", "created_at",
        )

    def get_suggested_skill_slugs(self, obj) -> list[str]:
        template = obj.template_section
        if template is None:
            return []
        value = template.suggested_skill_slugs or []
        return [s for s in value if isinstance(s, str)]


class ProjectSectionUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=False, max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=ProjectSectionStatus.choices, required=False,
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    output_snapshot = serializers.CharField(required=False, allow_blank=True)
    # Optional target position. The viewset re-orders sections in a safe
    # two-step swap to keep the (project, position) unique constraint
    # satisfied at every intermediate save.
    position = serializers.IntegerField(required=False, min_value=1)


class ProjectSectionCreateSerializer(serializers.Serializer):
    title = serializers.CharField(allow_blank=False, max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    position = serializers.IntegerField(required=False, min_value=1)


# ---------------------------------------------------------------------------
# Copilot message serializer
# ---------------------------------------------------------------------------

class CopilotMessageCreateSerializer(serializers.Serializer):
    content = serializers.CharField(allow_blank=False, max_length=4000)


class CopilotAutocompleteSerializer(serializers.Serializer):
    """
    Payload for inline ghost-text suggestions in the editor.

    Both ``before`` and ``after`` are caret windows of the editor's plain text
    representation. ``section_position`` and ``doc_title`` give the model an
    extra hint about which deliverable section / scratch doc the consultant is
    drafting.
    """

    before = serializers.CharField(allow_blank=True, max_length=8000)
    after = serializers.CharField(allow_blank=True, required=False, default="", max_length=4000)
    section_position = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    doc_title = serializers.CharField(required=False, allow_blank=True, max_length=255)


class InitializeStructureSerializer(serializers.Serializer):
    template_slug = serializers.SlugField()


# ---------------------------------------------------------------------------
# Deliverables (1:N within a project)
# ---------------------------------------------------------------------------


class ProjectDeliverableSerializer(serializers.ModelSerializer):
    template_slug = serializers.CharField(
        source="template.slug", read_only=True, default=None,
    )
    template_name = serializers.CharField(
        source="template.name", read_only=True, default=None,
    )
    sections_count = serializers.SerializerMethodField()
    completed_sections = serializers.SerializerMethodField()

    class Meta:
        model = ProjectDeliverable
        fields = (
            "id", "name", "slug",
            "template_slug", "template_name",
            "is_primary", "position", "status",
            "sections_count", "completed_sections",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "slug",
            "template_slug", "template_name",
            "sections_count", "completed_sections",
            "created_at", "updated_at",
        )

    def get_sections_count(self, obj) -> int:
        return obj.sections.count()

    def get_completed_sections(self, obj) -> int:
        return obj.sections.filter(status=ProjectSectionStatus.COMPLETED).count()


class ProjectDeliverableCreateSerializer(serializers.Serializer):
    """Payload for creating a new deliverable inside a project."""

    name = serializers.CharField(max_length=255)
    template_slug = serializers.SlugField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=ProjectDeliverableStatus.choices, required=False,
    )


class ProjectDeliverableUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, max_length=255)
    status = serializers.ChoiceField(
        choices=ProjectDeliverableStatus.choices, required=False,
    )
    is_primary = serializers.BooleanField(required=False)
    position = serializers.IntegerField(required=False, min_value=1)


# ---------------------------------------------------------------------------
# AI fill
# ---------------------------------------------------------------------------

class AiFillRequestSerializer(serializers.Serializer):
    """Request body for POST /projects/{slug}/ai-fill/."""

    fields = serializers.ListField(
        child=serializers.CharField(max_length=64),
        min_length=1,
        max_length=10,
        help_text=(
            "Claves de los campos a extraer del documento blueprint. "
            "Consultá GET /projects/ai-fill-fields/ para ver las claves disponibles."
        ),
    )

