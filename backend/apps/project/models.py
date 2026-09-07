from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.document.models import Document


class ProjectSectionStatus(models.TextChoices):
    NOT_STARTED = "not_started", _("Not Started")
    IN_PROGRESS = "in_progress", _("In Progress")
    REVIEW = "review", _("Review")
    COMPLETED = "completed", _("Completed")


class ProjectQuerySet(models.QuerySet):
    def for_user(self, user):
        if user.is_staff:
            return self
        return self.filter(Q(owner=user) | Q(shares__user=user)).distinct()


class Project(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="projects",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, blank=True, max_length=255)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    documents = models.ManyToManyField(
        Document,
        through="ProjectDocument",
        related_name="projects",
        blank=True,
    )
    blueprint_document = models.ForeignKey(
        Document,
        related_name="blueprint_for_projects",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Documento fuente central del proyecto (blueprint).",
    )
    enabled_skills = models.ManyToManyField(
        "skill.Skill",
        related_name="enabled_projects",
        blank=True,
        help_text="Skills/copilots shown in this project workspace.",
    )
    structure_template = models.ForeignKey(
        "ProjectStructureTemplate",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="projects",
        help_text="Structure template that defines the project's sections/phases.",
    )
    context_notes = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Persistent context injected into every copilot prompt. "
            'Example: {"company": "Acme Corp", "sector": "Manufacturing", '
            '"framework": "GRI", "reporting_year": "2024"}'
        ),
    )
    copilot_enabled = models.BooleanField(
        default=False,
        help_text="Whether the copilot assistant is active for this project.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ProjectQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("owner", "created_at")),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.owner})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def _generate_unique_slug(self) -> str:
        base = slugify(self.name) or "project"
        base = base[:255]
        slug = base
        counter = 1
        while Project.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            suffix = f"-{counter}"
            slug = f"{base[: 255 - len(suffix)]}{suffix}"
            counter += 1
        return slug

    def can_view(self, user) -> bool:
        if user.is_staff or self.owner_id == user.id:
            return True
        return self.shares.filter(user=user).exists()

    def can_edit(self, user) -> bool:
        if user.is_staff or self.owner_id == user.id:
            return True
        return self.shares.filter(
            user=user, role=ProjectShareRole.EDITOR
        ).exists()

    def can_manage_shares(self, user) -> bool:
        return user.is_staff or self.owner_id == user.id


class ProjectDocument(models.Model):
    project = models.ForeignKey(
        Project,
        related_name="project_documents",
        on_delete=models.CASCADE,
    )
    document = models.ForeignKey(
        Document,
        related_name="project_documents",
        on_delete=models.CASCADE,
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="added_project_documents",
        on_delete=models.SET_NULL,
    )
    is_primary = models.BooleanField(default=False)
    tags = models.ManyToManyField(
        "document.EvidenceTag",
        related_name="project_documents",
        blank=True,
        help_text=(
            "Qué papel cumple este documento en esta operación. Es lo que "
            "consultan los pasos que piden su evidencia por etiqueta. Se "
            "pre-carga desde los topics de la biblioteca al vincular, y desde "
            "ahí manda lo que decida el equipo de la operación. Vacío es un "
            "estado válido: el documento sigue entrando en los pasos que leen "
            "todo el expediente y en los que lo nombran explícitamente."
        ),
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("project", "document")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.project_id}-{self.document_id}"


class ProjectShareRole(models.TextChoices):
    VIEWER = "viewer", _("Viewer")
    EDITOR = "editor", _("Editor")


class ProjectShare(models.Model):
    project = models.ForeignKey(
        Project,
        related_name="shares",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="project_shares",
        on_delete=models.CASCADE,
    )
    role = models.CharField(
        max_length=20,
        choices=ProjectShareRole.choices,
        default=ProjectShareRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("project", "user")
        ordering = ("project", "user")

    def __str__(self) -> str:
        return f"{self.project_id}-{self.user_id}-{self.role}"


# ---------------------------------------------------------------------------
# Project structure templates + sections
# ---------------------------------------------------------------------------

class ProjectStructureTemplate(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="project_structure_templates",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Owner of the template. Null means global template.",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=255)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class ProjectStructureSection(models.Model):
    template = models.ForeignKey(
        ProjectStructureTemplate,
        related_name="sections",
        on_delete=models.CASCADE,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=1)
    suggested_skill_slugs = models.JSONField(
        default=list,
        blank=True,
        help_text="Slugs of skills the copilot can suggest for this section.",
    )

    class Meta:
        ordering = ("position",)
        unique_together = ("template", "position")

    def __str__(self) -> str:
        return f"{self.template.name} — {self.position}. {self.title}"


class ProjectDeliverableStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    FINAL = "final", _("Final")
    ARCHIVED = "archived", _("Archived")


class ProjectDeliverable(models.Model):
    """
    A single document/deliverable inside a project. Each project has at
    least one (the "primary") deliverable, but consultants frequently work
    on several documents per engagement (informe técnico, resumen
    ejecutivo, TdR cliente, etc.), so deliverables are first-class
    1:N children of ``Project``.

    Each deliverable owns its own ``ProjectSection`` set, copilot session,
    template and progress — they are independent of each other inside a
    project.
    """

    project = models.ForeignKey(
        Project,
        related_name="deliverables",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True)
    template = models.ForeignKey(
        ProjectStructureTemplate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="deliverables",
    )
    is_primary = models.BooleanField(
        default=False,
        help_text="Exactly one deliverable per project should be primary.",
    )
    position = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20,
        choices=ProjectDeliverableStatus.choices,
        default=ProjectDeliverableStatus.DRAFT,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "created_at")
        unique_together = (("project", "slug"), ("project", "position"))

    def __str__(self) -> str:
        return f"{self.project.name} — {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def _generate_unique_slug(self) -> str:
        base = slugify(self.name) or "entregable"
        base = base[:255]
        slug = base
        counter = 1
        qs = ProjectDeliverable.objects.filter(project=self.project)
        while qs.filter(slug=slug).exclude(pk=self.pk).exists():
            suffix = f"-{counter}"
            slug = f"{base[: 255 - len(suffix)]}{suffix}"
            counter += 1
        return slug


class ProjectSection(models.Model):
    # ``project`` is kept (denormalised) on the row for backward-compat
    # with consumers that read ``section.project`` directly. The
    # source-of-truth ownership chain is now Project -> Deliverable ->
    # Section; ``project`` is automatically synced from ``deliverable``
    # on save.
    project = models.ForeignKey(
        Project,
        related_name="sections",
        on_delete=models.CASCADE,
    )
    deliverable = models.ForeignKey(
        ProjectDeliverable,
        null=True,
        blank=True,
        related_name="sections",
        on_delete=models.CASCADE,
        help_text="Deliverable this section belongs to. Null only during "
                  "the migration window before back-filling.",
    )
    template_section = models.ForeignKey(
        ProjectStructureSection,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    position = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=20,
        choices=ProjectSectionStatus.choices,
        default=ProjectSectionStatus.NOT_STARTED,
    )
    notes = models.TextField(blank=True)
    output_snapshot = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("position",)
        unique_together = ("deliverable", "position")

    def __str__(self) -> str:
        return f"{self.project.name} — {self.position}. {self.title} ({self.status})"

    def save(self, *args, **kwargs):
        if self.deliverable_id and not self.project_id:
            self.project_id = self.deliverable.project_id
        super().save(*args, **kwargs)

