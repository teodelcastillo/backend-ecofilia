from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.document.models import Document


class RepositoryType(models.TextChoices):
    PUBLIC = "public", _("Public")
    PRIVATE = "private", _("Private")


class RepositoryQuerySet(models.QuerySet):
    def for_user(self, user):
        """Return PUBLIC repositories, owned PRIVATE repos, and shared PRIVATE repos."""
        from apps.user.access import public_reach_q

        if user.is_staff:
            return self
        return self.filter(
            (Q(repo_type=RepositoryType.PUBLIC) & public_reach_q(user))
            | Q(owner=user)
            | Q(shares__user=user)
        ).distinct()


class Repository(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="repositories",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Null for Ecofilia-managed PUBLIC repositories.",
    )
    repo_type = models.CharField(
        max_length=20,
        choices=RepositoryType.choices,
        default=RepositoryType.PRIVATE,
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, blank=True, max_length=255)
    description = models.TextField(blank=True)
    # For PUBLIC repos: mirrors an Ecofilia document category
    category = models.CharField(max_length=255, blank=True, null=True)
    documents = models.ManyToManyField(
        Document,
        through="RepositoryDocument",
        related_name="repositories",
        blank=True,
    )
    enabled_skills = models.ManyToManyField(
        "skill.Skill",
        related_name="enabled_repositories",
        blank=True,
        help_text="Skills/copilots shown in this repository workspace.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RepositoryQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("repo_type",)),
            models.Index(fields=("owner", "created_at")),
        ]
        verbose_name_plural = "repositories"

    def __str__(self) -> str:
        return f"{self.name} ({self.repo_type})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)

    def _generate_unique_slug(self) -> str:
        base = slugify(self.name) or "repository"
        base = base[:255]
        slug = base
        counter = 1
        while Repository.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            suffix = f"-{counter}"
            slug = f"{base[: 255 - len(suffix)]}{suffix}"
            counter += 1
        return slug

    def can_view(self, user) -> bool:
        from apps.user.access import public_reaches

        if user.is_staff:
            return True
        if self.repo_type == RepositoryType.PUBLIC and public_reaches(user, self.owner):
            return True
        if self.owner_id == user.id:
            return True
        return self.shares.filter(user=user).exists()

    def can_edit(self, user) -> bool:
        if user.is_staff:
            return True
        if self.repo_type == RepositoryType.PUBLIC:
            return False
        if self.owner_id == user.id:
            return True
        return self.shares.filter(
            user=user, role=RepositoryShareRole.EDITOR
        ).exists()

    def can_manage_shares(self, user) -> bool:
        if self.repo_type == RepositoryType.PUBLIC:
            return user.is_staff
        return user.is_staff or self.owner_id == user.id

    def can_manage_sources(self, user) -> bool:
        """Toggle active sources. Owners and editors can manage PRIVATE repos."""
        return self.can_edit(user)


class RepositoryShareRole(models.TextChoices):
    VIEWER = "viewer", _("Viewer")
    EDITOR = "editor", _("Editor")


class RepositoryShare(models.Model):
    repository = models.ForeignKey(
        Repository,
        related_name="shares",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="repository_shares",
        on_delete=models.CASCADE,
    )
    role = models.CharField(
        max_length=20,
        choices=RepositoryShareRole.choices,
        default=RepositoryShareRole.VIEWER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("repository", "user")
        ordering = ("repository", "user")

    def __str__(self) -> str:
        return f"{self.repository_id}-{self.user_id}-{self.role}"


class RepositoryDocument(models.Model):
    repository = models.ForeignKey(
        Repository,
        related_name="repository_documents",
        on_delete=models.CASCADE,
    )
    document = models.ForeignKey(
        Document,
        related_name="repository_documents",
        on_delete=models.CASCADE,
    )
    is_active = models.BooleanField(
        default=True,
        help_text="When False the document is excluded from chat and skill runs.",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("repository", "document")
        ordering = ("-added_at",)

    def __str__(self) -> str:
        return f"{self.repository_id}-{self.document_id} (active={self.is_active})"
