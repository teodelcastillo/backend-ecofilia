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


def _is_organization_admin(user) -> bool:
    from apps.user.access import is_organization_admin

    return is_organization_admin(user)


class ProjectQuerySet(models.QuerySet):
    def for_user(self, user):
        if user.is_staff:
            return self
        visible = Q(owner=user) | Q(shares__user=user)
        # El administrador de una institución ve todas sus operaciones.
        if _is_organization_admin(user):
            visible |= Q(organization_id=user.organization_id)
        return self.filter(visible).distinct()


class Project(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="projects",
        on_delete=models.CASCADE,
    )
    organization = models.ForeignKey(
        "user.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="projects",
        help_text=(
            "Institución a la que pertenece la operación. Define qué portal la "
            "muestra y qué administrador la ve entera. Se fija al crearla: la "
            "del dueño o, si la arma el equipo de Ecofilia, la del portal."
        ),
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
        # La operación de alguien de una institución es de esa institución,
        # se cree por donde se cree (API, admin, comandos).
        if self._state.adding and self.organization_id is None and self.owner_id:
            self.organization_id = (
                type(self.owner).objects.filter(pk=self.owner_id)
                .values_list("organization_id", flat=True).first()
            )
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

    def is_administered_by(self, user) -> bool:
        """Si ``user`` administra la institución de esta operación.

        El administrador de una institución (``role=admin`` con organización)
        tiene sobre todas sus operaciones lo mismo que el dueño: verlas,
        editarlas, compartirlas, ejecutar asistentes y borrarlas. Fuera de su
        institución, nada.
        """
        return bool(
            self.organization_id
            and _is_organization_admin(user)
            and self.organization_id == user.organization_id
        )

    def _owns(self, user) -> bool:
        return (
            user.is_staff
            or self.owner_id == user.id
            or self.is_administered_by(user)
        )

    def can_view(self, user) -> bool:
        if self._owns(user):
            return True
        return self.shares.filter(user=user).exists()

    def can_edit(self, user) -> bool:
        if self._owns(user):
            return True
        return self.shares.filter(
            user=user, role=ProjectShareRole.EDITOR
        ).exists()

    def can_manage_shares(self, user) -> bool:
        return self._owns(user)

    def can_delete(self, user) -> bool:
        """Borrar la operación es del dueño (o de quien administra), no de quien la edita.

        Un editor compartido trabaja la operación pero no decide que deje de
        existir: borrarla se lleva documentos vinculados, corridas e informe
        de todos los que participan.
        """
        return self._owns(user)


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
            "Etiquetas propias de esta operación. Sólo cuentan con "
            "`tags_overridden`; si no, el vínculo usa las del documento."
        ),
    )
    tags_overridden = models.BooleanField(
        default=False,
        help_text=(
            "La operación decidió sus propias etiquetas para este documento. "
            "Mientras sea falso, el documento aporta las suyas "
            "(`Document.evidence_tags`) y un cambio en la biblioteca llega solo."
        ),
    )
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("project", "document")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.project_id}-{self.document_id}"


class ProjectAlignmentSnapshot(models.Model):
    """
    Una foto de la determinación de alineación con París de una operación.

    La determinación vive en ``context_notes["alineacion_paris"]`` y cada
    edición la sobrescribe: sin estas fotos no hay forma de saber cómo estaba
    una operación en IDO y cómo quedó en DEC, ni cómo avanzó la cartera mes a
    mes. Se guarda una cada vez que cambia la determinación o la etapa, con la
    etapa en la que estaba la operación en ese momento.
    """

    class Source(models.TextChoices):
        EDIT = "edit", _("Edición")
        BACKFILL = "backfill", _("Estado al activar el historial")

    project = models.ForeignKey(
        Project, related_name="alignment_snapshots", on_delete=models.CASCADE
    )
    estado = models.CharField(max_length=40, blank=True)
    alineacion = models.JSONField(default=dict, blank=True)
    # La mirada del ejecutivo sobre el financiamiento verde de la operación
    # (``context_notes["enverdecimiento"]``): componentes que califican, con
    # sus montos, y oportunidades. Con esto el Resumen muestra cuán verde era
    # la operación en cada etapa, no sólo hoy.
    enverdecimiento = models.JSONField(default=dict, blank=True)
    monto = models.CharField(max_length=80, blank=True)
    captured_at = models.DateTimeField(db_index=True)
    captured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.EDIT)

    class Meta:
        ordering = ("captured_at", "id")
        indexes = [models.Index(fields=["project", "captured_at"])]

    def __str__(self) -> str:
        return f"{self.project_id} @ {self.captured_at:%Y-%m-%d} ({self.estado or 'sin etapa'})"


class ProjectStageTransition(models.Model):
    """
    Un cambio de etapa de una operación (pre-originación → IDO → DEC…).

    La etapa vive en ``context_notes["estado"]``, pero cambiarla no es editar
    un campo: entrar a IDO lanza el reporte de Ecofilia y el insumo para el
    ejecutivo. Este registro guarda quién la cambió, cuándo y qué se lanzó —o
    por qué no se pudo—, y es lo que permite reintentar después lo que quedó
    bloqueado (ver ``apps.project.services.stages``).
    """

    project = models.ForeignKey(
        Project, related_name="stage_transitions", on_delete=models.CASCADE
    )
    from_stage = models.CharField(max_length=40, blank=True)
    to_stage = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    # Por skill: {"skill_slug", "status": launched|exists|blocked|unavailable,
    # "execution_id", "reason"}. Se actualiza cuando un reintento lanza lo que
    # estaba bloqueado.
    launches = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=["project", "created_at"])]

    def __str__(self) -> str:
        return f"{self.project_id}: {self.from_stage or '—'} → {self.to_stage}"


class ProjectStageReportOrigin(models.TextChoices):
    # El ejecutivo trabaja sobre el reporte de Ecofilia: sus ediciones son
    # capas sobre la corrida, así que el original no se pierde.
    EDITADO = "editado", _("Editado sobre el reporte de Ecofilia")
    # Ecofilia redacta con las determinaciones y fundamentos del ejecutivo.
    GUIADO = "guiado", _("Guiado con el criterio del ejecutivo")
    # El ejecutivo lo escribió afuera y lo subió.
    SUBIDO = "subido", _("Subido por el ejecutivo")


class ProjectStageReport(models.Model):
    """
    El informe del ejecutivo para una etapa de la operación.

    Coexiste con el reporte de Ecofilia: aquel es la base objetiva; éste es la
    versión oficial, que el ejecutivo arma por una de tres vías y después
    **emite**. Emitido, queda inmutable —el snapshot no cambia aunque la corrida
    se siga editando— y visible para toda la institución en modo lectura.

    Hay a lo sumo uno vigente (no anulado) por etapa. Hasta emitirlo el
    ejecutivo puede cambiar de vía; emitido, sólo el staff lo anula, y anularlo
    conserva el registro.
    """

    project = models.ForeignKey(
        Project, related_name="stage_reports", on_delete=models.CASCADE
    )
    stage = models.CharField(max_length=40)
    origin = models.CharField(max_length=20, choices=ProjectStageReportOrigin.choices)

    # Editado o guiado: la corrida cuyo informe (con sus ediciones) es el del
    # ejecutivo. En los dos casos es la IET de Ecofilia: el guiado escribe
    # sobre ella como borradores.
    execution = models.ForeignKey(
        "skill.SkillExecution",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    # Subido: el documento del ejecutivo. No se vincula al expediente de la
    # operación a propósito: si lo estuviera, las corridas de Ecofilia leerían
    # la conclusión del ejecutivo como evidencia.
    document = models.ForeignKey(
        Document,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    # Guiado: la corrida que redactó, con el criterio del ejecutivo, las
    # secciones que cambian (C, D y E). Su resultado no es un informe aparte:
    # se vuelca como borradores sobre ``execution`` (la IET de Ecofilia).
    guided_execution = models.ForeignKey(
        "skill.SkillExecution",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    # Qué hizo el guiado al volcarse: secciones reescritas y sin cambios.
    guided_result = models.JSONField(default=dict, blank=True)
    # La lectura estructurada del documento subido (determinaciones,
    # recomendaciones, consideraciones), para compararlo con Ecofilia.
    reader_execution = models.ForeignKey(
        "skill.SkillExecution",
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    emitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    emitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    # El informe tal como se emitió. Para los que viven en una corrida es el
    # markdown ensamblado (carátula, insumo y secciones con sus ediciones);
    # para el subido, vacío: el documento mismo es el emitido.
    emitted_markdown = models.TextField(blank=True)
    # La mirada estructurada del ejecutivo al emitir: determinaciones y
    # fundamentos por criterio, o la lectura del documento subido.
    emitted_assessment = models.JSONField(default=dict, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="+",
        on_delete=models.SET_NULL,
    )
    void_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "stage"],
                condition=Q(voided_at__isnull=True),
                name="project_stage_report_one_active",
            )
        ]

    def __str__(self) -> str:
        return f"{self.project_id} {self.stage} ({self.origin})"

    @property
    def is_emitted(self) -> bool:
        return self.emitted_at is not None and self.voided_at is None


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

