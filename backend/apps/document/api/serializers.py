import json

from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils.text import slugify
from apps.document.models import (
    SmartChunk,
    Document,
    DocumentShare,
    DocumentShareRole,
    Category,
    EvidenceTag,
)
from apps.document.category_utils import category_ancestor_path, resolve_write_category
from apps.user.models import UserRole

User = get_user_model()


class TopicListField(serializers.Field):
    """
    Accepts either a JSON array or a semicolon/comma-separated string.

    Examples — all result in ["ndc", "argentina", "latam"]:
        ["ndc", "argentina", "latam"]   ← preferred (JSON array)
        "ndc; argentina; latam"
        "ndc, argentina, latam"
    """

    default_error_messages = {
        "invalid": "Expected a list or a semicolon/comma-separated string.",
    }

    def get_value(self, dictionary):
        # En multipart el campo puede venir repetido, un tema por valor.
        if hasattr(dictionary, "getlist"):
            values = dictionary.getlist(self.field_name)
            if len(values) > 1:
                return values
        return super().get_value(dictionary)

    def to_internal_value(self, data):
        if data is None:
            return []
        if isinstance(data, str):
            text = data.strip()
            # `apiClient.upload` serializa los arrays como JSON.
            if text.startswith("["):
                try:
                    data = json.loads(text)
                except ValueError:
                    self.fail("invalid")
            else:
                parts = [t.strip() for t in text.replace(",", ";").split(";")]
                return [p for p in parts if p]
        if isinstance(data, (list, tuple)):
            return [str(t).strip() for t in data if str(t).strip()]
        self.fail("invalid")

    def to_representation(self, value):
        return value if value is not None else []


class EvidenceTagSlugsField(serializers.Field):
    """
    Etiquetas de evidencia de un documento, por slug.

    Llegan de tres formas según quién sube: una lista JSON (edición), un
    campo repetido en multipart (subida masiva) o un string con la lista en
    JSON o separada por comas (``apiClient.upload`` serializa los arrays así).
    Todas terminan en la lista de ``EvidenceTag`` que existen; un slug
    desconocido es un error, no se descarta en silencio: el documento
    quedaría sin la etiqueta que su autor creyó ponerle.
    """

    default_error_messages = {
        "invalid": "Mandá una lista de slugs de etiquetas.",
        "unknown": "Etiquetas inexistentes: {slugs}.",
    }

    def get_value(self, dictionary):
        if hasattr(dictionary, "getlist"):
            values = dictionary.getlist(self.field_name)
            if len(values) > 1:
                return values
        return super().get_value(dictionary)

    def to_internal_value(self, data):
        if data is None:
            return []
        if isinstance(data, str):
            text = data.strip()
            if text.startswith("["):
                try:
                    data = json.loads(text)
                except ValueError:
                    self.fail("invalid")
            else:
                data = [part for part in text.split(",")]
        if isinstance(data, (list, tuple)) and len(data) == 1 and isinstance(data[0], str) and data[0].strip().startswith("["):
            return self.to_internal_value(data[0])
        if not isinstance(data, (list, tuple)):
            self.fail("invalid")
        slugs = list(dict.fromkeys(str(s).strip() for s in data if str(s).strip()))
        found = {t.slug: t for t in EvidenceTag.objects.filter(slug__in=slugs)}
        missing = [slug for slug in slugs if slug not in found]
        if missing:
            self.fail("unknown", slugs=", ".join(missing))
        return [found[slug] for slug in slugs]

    def to_representation(self, value):
        return [tag.slug for tag in value.all()]


def _normalize_topics(raw_topics) -> list[str]:
    """Temas sin repetir, recortados y en minúsculas."""
    seen = set()
    clean = []
    for t in raw_topics or []:
        normalized = t.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            clean.append(normalized)
    return clean


def _can_manage_public_documents(user) -> bool:
    if not user:
        return False
    return bool(user.is_superuser or getattr(user, "role", None) == UserRole.ADMIN)

class SmartChunkSerializer(serializers.ModelSerializer):
    class Meta:
        model = SmartChunk
        fields = [
            'id',
            'content',
            'chunk_index',
            'document_id',
            'token_count',
            'embedding',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at', "content_norm"]


class DocumentChunkSerializer(serializers.ModelSerializer):
    """Chunk payload enriched with the document data needed by citation popups."""
    document_slug = serializers.CharField(source='document.slug', read_only=True)
    document_name = serializers.CharField(source='document.name', read_only=True)
    document_file = serializers.SerializerMethodField()
    document_source = serializers.CharField(source='document.source', read_only=True)

    class Meta:
        model = SmartChunk
        fields = [
            'id',
            'content',
            'chunk_index',
            'document_id',
            'document_slug',
            'document_name',
            'document_file',
            'document_source',
            'title',
            'summary',
            'keywords',
            'context_summary',
            'token_count',
            'created_at',
        ]
        read_only_fields = fields

    def get_document_file(self, obj):
        if not obj.document.file:
            return None
        request = self.context.get('request')
        url = obj.document.file.url
        if request:
            return request.build_absolute_uri(url)
        return url


class DocumentCreateSerializer(serializers.ModelSerializer):
    file = serializers.FileField(required=True, allow_null=False, allow_empty_file=False)
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    category = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    category_slug = serializers.SlugField(
        required=False, allow_blank=True, write_only=True,
        help_text="Preferred: assign document to this category by slug (owner must match).",
    )
    description = serializers.CharField(required=False, allow_blank=True)
    is_public = serializers.BooleanField(required=False, default=False)
    project_slug = serializers.SlugField(
        write_only=True, required=False, allow_blank=True,
    )
    # El diálogo de subida mandaba temas, año, región y fuente desde siempre,
    # pero este serializador no los declaraba y DRF los descartaba sin decir
    # nada: el documento llegaba a la biblioteca sin la metadata que su autor
    # había cargado.
    topics = TopicListField(required=False, allow_null=True)
    year = serializers.IntegerField(required=False, allow_null=True)
    region = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    source = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    evidence_tags = EvidenceTagSlugsField(required=False)

    class Meta:
        model = Document
        fields = [
            'file',
            'name',
            'category',
            'category_slug',
            'description',
            'is_public',
            'project_slug',
            'topics',
            'year',
            'region',
            'source',
            'evidence_tags',
        ]

    def validate_file(self, value):
        if not value:
            raise serializers.ValidationError("File is required.")
        return value

    def validate_project_slug(self, value):
        if not value:
            return value
        from apps.project.models import Project
        try:
            project = Project.objects.get(slug=value)
        except Project.DoesNotExist:
            raise serializers.ValidationError("Project not found.")
        request = self.context.get('request')
        if request and not project.can_edit(request.user):
            raise serializers.ValidationError(
                "You do not have permission to add documents to this project."
            )
        self._project = project
        return value

    def validate_is_public(self, value):
        # Public visibility can only be changed after upload in document edit flows.
        if value:
            raise serializers.ValidationError(
                "is_public can only be set on already uploaded documents."
            )
        return value

    def create(self, validated_data):
        validated_data.pop('project_slug', None)
        request = self.context.get('request')
        user = request.user
        is_staff = user.is_staff
        category_slug = validated_data.pop('category_slug', None)
        category_text = validated_data.pop('category', None)
        if category_slug and str(category_slug).strip():
            try:
                cat, cat_str = resolve_write_category(
                    user,
                    category_slug=category_slug,
                    is_staff=is_staff,
                )
            except ValueError as e:
                if str(e) == "category_not_found":
                    raise serializers.ValidationError(
                        {"category_slug": "Category not found."},
                    ) from e
                raise serializers.ValidationError(
                    {"category_slug": "You do not have permission to use this category."},
                ) from e
            validated_data['category_ref'] = cat
            validated_data['category'] = cat_str
        else:
            try:
                cat, cat_str = resolve_write_category(
                    user,
                    category_name=category_text,
                    is_staff=is_staff,
                )
            except ValueError as e:
                if str(e) == "category_not_found":
                    raise serializers.ValidationError(
                        {"category": "Category not found."},
                    ) from e
                raise serializers.ValidationError(
                    {"category": "You do not have permission to use this category."},
                ) from e
            validated_data['category_ref'] = cat
            validated_data['category'] = cat_str
        if 'topics' in validated_data:
            validated_data['topics'] = _normalize_topics(validated_data['topics'])
        # `evidence_tags` lo asigna `ModelSerializer.create` después de crear
        # la fila (es muchos-a-muchos).
        return super().create(validated_data)


class DocumentBulkPublicSerializer(serializers.Serializer):
    """Superusers only: bulk set is_public (public library visibility)."""

    slugs = serializers.ListField(
        child=serializers.SlugField(),
        min_length=1,
        max_length=200,
    )
    is_public = serializers.BooleanField()


class DocumentBulkEvidenceTagsSerializer(serializers.Serializer):
    """Superadmins: agregar y/o quitar etiquetas de evidencia a muchos documentos."""

    slugs = serializers.ListField(child=serializers.SlugField(), min_length=1, max_length=500)
    add = EvidenceTagSlugsField(required=False)
    remove = EvidenceTagSlugsField(required=False)

    def validate(self, attrs):
        if not attrs.get("add") and not attrs.get("remove"):
            raise serializers.ValidationError("Indicá etiquetas para agregar o para quitar.")
        return attrs


class DocumentBulkCreateSerializer(serializers.Serializer):
    files = serializers.ListField(
        child=serializers.FileField(required=True, allow_null=False, allow_empty_file=False),
        required=True,
        min_length=1,
        max_length=100,
    )
    name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    category = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    category_slug = serializers.SlugField(required=False, allow_blank=True, write_only=True)
    description = serializers.CharField(required=False, allow_blank=True)
    is_public = serializers.BooleanField(required=False, default=False)
    project_slug = serializers.SlugField(
        write_only=True, required=False, allow_blank=True,
    )
    topics = TopicListField(required=False, allow_null=True)
    year = serializers.IntegerField(required=False, allow_null=True)
    region = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    source = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=255)
    evidence_tags = EvidenceTagSlugsField(required=False)

    def validate_files(self, value):
        if not value or len(value) == 0:
            raise serializers.ValidationError("At least one file is required.")
        for file in value:
            if not file:
                raise serializers.ValidationError("All files must be provided and not empty.")
            if file.size == 0:
                raise serializers.ValidationError("Files cannot be empty.")
        return value

    def validate_project_slug(self, value):
        if not value:
            return value
        from apps.project.models import Project
        try:
            project = Project.objects.get(slug=value)
        except Project.DoesNotExist:
            raise serializers.ValidationError("Project not found.")
        request = self.context.get('request')
        if request and not project.can_edit(request.user):
            raise serializers.ValidationError(
                "You do not have permission to add documents to this project."
            )
        self._project = project
        return value

    def validate_is_public(self, value):
        # Public visibility can only be changed after upload in document edit flows.
        if value:
            raise serializers.ValidationError(
                "is_public can only be set on already uploaded documents."
            )
        return value

class DocumentSerializer(serializers.ModelSerializer):
    """Serializer for listing documents - read-only fields"""
    is_public = serializers.BooleanField(read_only=True)
    evidence_tags = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="slug"
    )
    is_owner = serializers.SerializerMethodField()
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    category_slug = serializers.SerializerMethodField()
    category_path = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id',
            'slug',
            'name',
            'category',
            'category_slug',
            'category_path',
            'description',
            'content_summary',
            'file',
            'is_public',
            'is_owner',
            'owner_email',
            'created_at',
            'chunking_status',
            'chunking_done',
            'last_error',
            'page_count',
            'pages_with_text',
            'topics',
            'year',
            'region',
            'source',
            'evidence_tags',
        ]
        read_only_fields = [
            'id',
            'slug',
            'created_at',
            'chunking_status',
            'chunking_done',
            'last_error',
            'page_count',
            'pages_with_text',
            'is_public',
            'owner',
            'name',
            'category',
            'description',
            'content_summary',
            'topics',
            'year',
            'region',
            'source',
        ]
    
    def get_is_owner(self, obj):
        """Indica si el usuario actual es el propietario del documento"""
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            return obj.owner == request.user
        return False

    def get_category_slug(self, obj):
        if obj.category_ref_id and obj.category_ref:
            return obj.category_ref.slug
        return None

    def get_category_path(self, obj):
        if not obj.category_ref_id:
            return []
        # May be lazy; ensure ref is present
        ref = obj.category_ref if obj.category_ref_id else None
        if not ref:
            return []
        return [{"name": n, "slug": s} for n, s in category_ancestor_path(ref)]


class DocumentListSerializer(DocumentSerializer):
    """El mismo documento, pero sin el resumen automático.

    ``content_summary`` es el resumen que genera la ingesta sobre el archivo
    entero: texto largo, pensado para RAG y para sugerir documentos
    relacionados. En un listado no lo mira nadie —ninguna vista del frontend lo
    consume— y multiplica el tamaño de cada respuesta por la cantidad de
    documentos del usuario.

    Se deja fuera acá y no en ``DocumentSerializer`` porque ese mismo
    serializador responde altas y ediciones de un documento suelto, donde
    devolverlo no cuesta nada y sacarlo sí sería un cambio de contrato.
    """

    class Meta(DocumentSerializer.Meta):
        fields = [f for f in DocumentSerializer.Meta.fields if f != "content_summary"]
        read_only_fields = [
            f for f in DocumentSerializer.Meta.read_only_fields if f != "content_summary"
        ]


class DocumentDetailSerializer(serializers.ModelSerializer):
    """Serializer for retrieving a single document with all fields"""
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    evidence_tags = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="slug"
    )
    category_slug = serializers.SerializerMethodField()
    category_path = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id',
            'slug',
            'name',
            'category',
            'category_slug',
            'category_path',
            'description',
            'content_summary',
            'file',
            'created_at',
            'chunking_status',
            'chunking_done',
            'is_public',
            'owner_email',
            'topics',
            'year',
            'region',
            'source',
            'evidence_tags',
        ]
        read_only_fields = [
            'id',
            'slug',
            'created_at',
            'chunking_status',
            'chunking_done',
            'owner_email',
            'content_summary',
        ]

    def get_category_slug(self, obj):
        if obj.category_ref_id and obj.category_ref:
            return obj.category_ref.slug
        return None

    def get_category_path(self, obj):
        if not obj.category_ref_id:
            return []
        ref = obj.category_ref if obj.category_ref_id else None
        if not ref:
            return []
        return [{"name": n, "slug": s} for n, s in category_ancestor_path(ref)]


class DocumentUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating document metadata"""
    is_public = serializers.BooleanField(required=False)
    category_slug = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, write_only=True,
    )
    topics = TopicListField(required=False, allow_null=True)
    evidence_tags = EvidenceTagSlugsField(required=False)

    class Meta:
        model = Document
        fields = [
            'name',
            'category',
            'category_slug',
            'description',
            'is_public',
            'topics',
            'year',
            'region',
            'source',
            'evidence_tags',
        ]
    
    def validate_is_public(self, value):
        """Only superadmins and admin users can modify is_public field."""
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            if not _can_manage_public_documents(request.user):
                raise serializers.ValidationError(
                    "Only superadmins and admin users can modify the is_public field."
                )
        return value
    
    def update(self, instance, validated_data):
        """Update document, but restrict is_public to superadmins/admin users."""
        request = self.context.get('request')
        user = request.user if request and hasattr(request, "user") else None
        is_staff = user.is_staff if user else False

        category_changed = False
        new_ref = None
        new_cat = None

        if "category_slug" in validated_data:
            category_changed = True
            validated_data.pop("category", None)
            slug_val = validated_data.pop("category_slug")
            if slug_val is None or (isinstance(slug_val, str) and not str(slug_val).strip()):
                new_ref, new_cat = None, None
            else:
                try:
                    cat, cat_str = resolve_write_category(
                        user,
                        category_slug=str(slug_val).strip(),
                        is_staff=is_staff,
                    )
                except ValueError as e:
                    if str(e) == "category_not_found":
                        raise serializers.ValidationError(
                            {"category_slug": "Category not found."},
                        ) from e
                    raise serializers.ValidationError(
                        {"category_slug": "You do not have permission to use this category."},
                    ) from e
                new_ref, new_cat = cat, cat_str
        elif "category" in validated_data:
            category_changed = True
            ctext = validated_data.pop("category", None)
            if ctext is None or (isinstance(ctext, str) and not str(ctext).strip()):
                new_ref, new_cat = None, None
            else:
                try:
                    cat, cat_str = resolve_write_category(
                        user,
                        category_name=str(ctext).strip(),
                        is_staff=is_staff,
                    )
                except ValueError as e:
                    if str(e) == "category_not_found":
                        raise serializers.ValidationError(
                            {"category": "Category not found."},
                        ) from e
                    raise serializers.ValidationError(
                        {"category": "You do not have permission to use this category."},
                    ) from e
                new_ref, new_cat = cat, cat_str

        # Remove is_public from validated_data if user is not superuser
        if request and hasattr(request, 'user'):
            if not _can_manage_public_documents(request.user) and 'is_public' in validated_data:
                validated_data.pop('is_public')

        # Normalize topics: null → empty list, deduplicate, strip, lowercase
        topics_changed = 'topics' in validated_data
        if topics_changed:
            validated_data['topics'] = _normalize_topics(validated_data.pop('topics'))
        tags_sent = 'evidence_tags' in validated_data

        instance = super().update(instance, validated_data)

        # Un documento al que le cargan temas pero todavía ninguna etiqueta
        # recibe la que sugieren esos temas. Sólo si no tiene ninguna y no se
        # mandaron en este mismo pedido: una etiqueta elegida a mano —o
        # vaciada a propósito— no se toca.
        if topics_changed and not tags_sent and not instance.evidence_tags.exists():
            from apps.document.services import default_evidence_tags_for

            proposed = default_evidence_tags_for(instance)
            if proposed:
                instance.evidence_tags.set(proposed)
        if category_changed:
            instance.category_ref = new_ref
            instance.category = new_cat
            instance.save(update_fields=["category_ref", "category"])
        return instance


class DocumentShareSerializer(serializers.ModelSerializer):
    """Serializer for reading document shares"""
    user_email = serializers.EmailField(source="user.email", read_only=True)
    
    class Meta:
        model = DocumentShare
        fields = ("id", "user", "user_email", "role", "created_at")
        read_only_fields = ("id", "user_email", "created_at")


class DocumentShareRoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=DocumentShareRole.choices)


class DocumentShareWriteSerializer(serializers.Serializer):
    """Serializer for creating/updating document shares"""
    user_email = serializers.EmailField()
    role = serializers.ChoiceField(choices=DocumentShareRole.choices)
    
    def validate(self, attrs):
        """Valida el email y obtiene el usuario"""
        email = attrs.get('user_email')
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise serializers.ValidationError({
                'user_email': f"No existe un usuario con el email: {email}"
            })
        
        document = self.context.get("document")
        if document and document.owner == user:
            raise serializers.ValidationError({
                'user_email': "El propietario del documento no puede ser compartido."
            })
        
        attrs['user'] = user
        return attrs


class CategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = (
            'id', 'slug', 'name', 'parent', 'children',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'slug', 'created_at', 'updated_at')

    def get_children(self, obj):
        qs = obj.children.all()
        if not qs.exists():
            return []
        return CategorySerializer(qs, many=True).data


class CategoryWriteSerializer(serializers.ModelSerializer):
    parent_slug = serializers.SlugField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = Category
        fields = ('name', 'parent_slug')

    def validate_parent_slug(self, value):
        if not value:
            return value
        try:
            parent = Category.objects.get(slug=value)
        except Category.DoesNotExist:
            raise serializers.ValidationError("Parent category not found.")
        request = self.context.get('request')
        if request and parent.owner != request.user and not request.user.is_staff:
            raise serializers.ValidationError("You do not own this parent category.")
        self._parent = parent
        return value

    def create(self, validated_data):
        validated_data.pop('parent_slug', None)
        parent = getattr(self, '_parent', None)
        if parent:
            validated_data['parent'] = parent
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('parent_slug', None)
        parent = getattr(self, '_parent', None)
        if parent:
            validated_data['parent'] = parent
        return super().update(instance, validated_data)

class EvidenceTagSerializer(serializers.ModelSerializer):
    """Etiqueta del catálogo con el que los pasos piden su evidencia."""

    document_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = EvidenceTag
        fields = (
            "id",
            "slug",
            "name",
            "description",
            "source_topics",
            "is_seed",
            "position",
            "document_count",
        )
        read_only_fields = ("id", "slug", "is_seed", "document_count")


class EvidenceTagWriteSerializer(serializers.ModelSerializer):
    """
    Alta de etiquetas nuevas.

    El slug se deriva del nombre y no se acepta del cliente: es el contrato que
    queda escrito en la definición de los workflows, así que no puede depender
    de lo que alguien tipee en un formulario. `is_seed` tampoco: distingue las
    etiquetas que provee Ecofilia y sólo se pone en las migraciones de semilla.
    """

    class Meta:
        model = EvidenceTag
        fields = ("name", "description", "source_topics", "position")

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("El nombre no puede estar vacío.")
        slug = slugify(name)[:80]
        if not slug:
            raise serializers.ValidationError(
                "El nombre tiene que producir un identificador válido."
            )
        qs = EvidenceTag.objects.filter(slug=slug)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(f"Ya existe una etiqueta «{slug}».")
        return name
