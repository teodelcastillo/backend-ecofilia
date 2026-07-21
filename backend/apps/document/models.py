import uuid
from django.db import models
from django.db.models import QuerySet
from django.utils.text import slugify
from django.db.models import Func, F, TextField, GeneratedField, Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from pgvector.django import VectorField, CosineDistance
from django.contrib.postgres.fields import ArrayField
from django.contrib.auth import get_user_model
from apps.document.utils.client_openia import embed_text
import os
import re
User = get_user_model()

class ChunkingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    DONE = "done", "Done"
    ERROR = "error", "Error"


class Document(models.Model):
    owner = models.ForeignKey(User, related_name="document_owner", on_delete=models.CASCADE)
    name = models.CharField(max_length=255, blank=True)
    slug = models.SlugField(unique=True, blank=True)
    # Kept for backward-compatible API clients; prefer `category_ref` / `category_slug` in new code.
    category = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Legacy display string; mirrors Category.name when category_ref is set.",
    )
    category_ref = models.ForeignKey(
        "Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
    )
    description = models.TextField(blank=True)
    content_summary = models.TextField(
        blank=True,
        help_text=(
            "Resumen automático del archivo al ingerirlo (para RAG y documentos relacionados). "
            "Distinto del campo `description`, que suele ser manual."
        ),
    )
    file = models.FileField(upload_to='documents/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    extracted_text = models.TextField(blank=True)
    chunking_status = models.CharField(
        max_length=20,
        choices=ChunkingStatus.choices,
        default=ChunkingStatus.PENDING,
    )
    chunking_offset = models.IntegerField(default=0)
    chunking_done = models.BooleanField(default=False)
    last_error = models.TextField(blank=True)
    retry_count = models.IntegerField(default=0)
    is_public = models.BooleanField(default=False)
    year = models.IntegerField(blank=True, null=True, help_text="Año del documento")
    region = models.CharField(max_length=255, blank=True, null=True, help_text="Región del documento")
    topics = ArrayField(
        base_field=models.TextField(),
        blank=True,
        default=list,
        help_text="Temas o palabras clave del documento",
    )
    source = models.CharField(max_length=255, blank=True, null=True, help_text="Fuente del documento")

    def save(self, *args, **kwargs):
        # Si no hay name ni slug, generar ambos desde el nombre del archivo
        if not self.name and not self.slug:
            name = os.path.basename(self.file.name)
            name = os.path.splitext(name)[0] 
            self.name = name[:255]
            base_slug = slugify(name[:50])
            slug = base_slug
            counter = 1
            while Document.objects.filter(slug=slug).exists():
                s_counter = str(counter)
                slug = base_slug[:49-len(s_counter)]
                slug = f"{base_slug}-"+ s_counter
                counter += 1
            self.slug = slug
        # Si hay name pero no slug, generar slug desde el name proporcionado
        elif self.name and not self.slug:
            base_slug = slugify(self.name[:50])
            slug = base_slug
            counter = 1
            while Document.objects.filter(slug=slug).exists():
                s_counter = str(counter)
                slug = base_slug[:49-len(s_counter)]
                slug = f"{base_slug}-"+ s_counter
                counter += 1
            self.slug = slug
        # Si hay slug pero no name, y hay archivo, generar name desde el archivo
        elif self.slug and not self.name and self.file:
            name = os.path.basename(self.file.name)
            name = os.path.splitext(name)[0] 
            self.name = name[:255]

        if self.topics:
            normalized = []
            for item in self.topics:
                if not item:
                    continue
                for part in re.split(r"[;,|]+", item):
                    part = part.lower().strip()
                    if part and part not in normalized:
                        normalized.append(part)
            self.topics = normalized

        if self.category_ref_id:
            from django.apps import apps

            CategoryModel = apps.get_model("document", "Category")
            try:
                cat = CategoryModel.objects.only("name").get(pk=self.category_ref_id)
                self.category = cat.name
            except CategoryModel.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    def can_view(self, user) -> bool:
        """Verifica si el usuario puede ver el documento"""
        if user.is_staff or self.owner_id == user.id:
            return True
        if self.is_public:
            return True
        if self.shares.filter(user=user).exists():
            return True
        # Align with list/retrieve/RAG: project collaborators may read linked docs.
        from apps.project.models import ProjectShare

        shared_project_ids = ProjectShare.objects.filter(user=user).values_list(
            "project_id", flat=True
        )
        return self.projects.filter(id__in=shared_project_ids).exists()

    def can_edit(self, user) -> bool:
        """Verifica si el usuario puede editar el documento"""
        if user.is_staff or self.owner_id == user.id:
            return True
        return self.shares.filter(
            user=user, role=DocumentShareRole.EDITOR
        ).exists()

    def can_manage_shares(self, user) -> bool:
        """Verifica si el usuario puede gestionar los shares del documento"""
        return user.is_staff or self.owner_id == user.id

    def __str__(self):
        return f'{self.id}-{self.name}'

class SmartChunkQuerySet(QuerySet):
    def top_similar(self, text: str, top_n=5):
        if not text:
            return self.none()

        query_embedding = embed_text(text)
        if not query_embedding:
            return self.none()
        numbers = [num for num in re.findall(r"\d+\.?\d*", text)]
        qs = self.annotate(
            distance=CosineDistance("embedding", query_embedding)
        )
        if numbers:
            q = Q()
            for num in numbers:
                q |= Q(content_norm__icontains=num)
            candidates = self.filter(q).only("id")[:1000]
            qs = qs.filter(id__in=candidates)

        return qs.order_by("distance")[:top_n]


    def top_similar2(self, text: str, top_n=5):
        if not text:
            return self.none()

        query_embedding = embed_text(text)
        if not query_embedding:
            return self.none()

        return self.annotate(
            distance=CosineDistance("embedding", query_embedding)
        ).order_by("distance")[:top_n]

class SmartChunk(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name='chunks')
    chunk_index = models.IntegerField()
    content = models.TextField()
    content_norm = GeneratedField(
        expression=Func(Lower(F("content")), function="immutable_unaccent"),
        output_field=TextField(),
        db_persist=True,     # matches STORED
        editable=False
    )
    token_count = models.IntegerField()
    title = models.CharField(max_length=255, blank=True, null=True)
    summary = models.TextField(blank=True)
    keywords = ArrayField(models.TextField(), blank=True, default=list)
    context_summary = models.TextField(
        blank=True,
        default="",
    )
    embedding = VectorField(dimensions=1536, blank=True, null=True)
    page_number = models.IntegerField(
        null=True,
        blank=True,
        help_text="Página del documento de origen (1-based). Null para docs sin información de página.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("document", "chunk_index")]



    class SmartChunkManager(models.Manager):
        def get_queryset(self):
            return SmartChunkQuerySet(self.model, using=self._db)

        # Optional shortcut to call on manager directly
        def top_similar(self, *args, **kwargs):
            return self.get_queryset().top_similar(*args, **kwargs)

        def top_similar2(self, *args, **kwargs):
            return self.get_queryset().top_similar2(*args, **kwargs)

    # class SmartChunkManager(models.Manager):
    #     #  def get_queryset(self):
    #     #     return super().get_queryset()
         
    #      def get_queryset(self, text, top_n=5):
    #         qs = self.get_queryset()
    #         if not text:<
    #             return qs.none()
    #         query_embedding = embed_text(text)
    #         if not query_embedding:
    #             return qs.none()
    #         return qs.annotate(
    #             distance=CosineDistance("embedding", query_embedding)
    #         ).order_by("distance")[:top_n]

    objects = SmartChunkManager()
    def __str__(self):
        return f"{self.id}-{self.document.name}"


class DocumentShareRole(models.TextChoices):
    VIEWER = "viewer", _("Viewer")
    EDITOR = "editor", _("Editor")


class DocumentShare(models.Model):
    document = models.ForeignKey(
        Document,
        related_name="shares",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        User,
        related_name="document_shares",
        on_delete=models.CASCADE,
    )
    role = models.CharField(
        max_length=20,
        choices=DocumentShareRole.choices,
        default=DocumentShareRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("document", "user")
        ordering = ("document", "user")

    def __str__(self):
        return f"{self.document_id}-{self.user_id}-{self.role}"


class Category(models.Model):
    owner = models.ForeignKey(
        User,
        related_name="categories",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, blank=True, max_length=255)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="children",
        on_delete=models.CASCADE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "category"
            base = base[:255]
            slug = base
            counter = 1
            while Category.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                suffix = f"-{counter}"
                slug = f"{base[:255 - len(suffix)]}{suffix}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)
