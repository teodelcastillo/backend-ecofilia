from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserRole(models.TextChoices):
    ADMIN = "admin", _("Administrator")
    MANAGER = "manager", _("Manager")
    MEMBER = "member", _("Member")


# Feature slugs the frontend understands for sidebar/section gating.
# Kept as data (not choices) so adding a feature doesn't need a migration.
KNOWN_FEATURES = [
    "projects",
    "library",
    "repositories",
    "chat",
    "skills",
    "evaluations",
    "tableros",
    "inventories",
]


class Organization(models.Model):
    """
    Client organization (e.g. CAF). Groups users and defines their
    entitlements: which sections of the app they see and which skills
    (workflow agents) Ecofilia has enabled for them.
    """

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=64, unique=True)
    restricted = models.BooleanField(
        default=False,
        help_text=_(
            "When true, members only see the sections in enabled_features and "
            "the skills in enabled_skills; they cannot create their own skills "
            "or evaluations."
        ),
    )
    enabled_features = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            'Sections visible to members of a restricted org, e.g. '
            '["projects", "library", "chat"]. Ignored when restricted=False.'
        ),
    )
    enabled_skills = models.ManyToManyField(
        "skill.Skill",
        blank=True,
        related_name="enabled_for_organizations",
        help_text=_(
            "Skills (including workflow agents) enabled for members of a "
            "restricted org. Ignored when restricted=False."
        ),
    )
    default_project_skills = models.ManyToManyField(
        "skill.Skill",
        blank=True,
        related_name="default_for_organizations",
        help_text=_(
            "Skills every NEW project of this org starts with. Distinct from "
            "enabled_skills, which only controls visibility: a restricted org "
            "may see several agents but have only one attached by default. "
            "Members of restricted orgs cannot assign skills themselves, so "
            "without this their projects would be born with an empty workspace."
        ),
    )
    # Lo que hace distinto al portal de esta institución: marca, etapas,
    # lanzamientos, roles de los workflows, grupos de etiquetas y criterios.
    # Vacío: rige la configuración base. Se lee con
    # ``apps.user.institution_config.effective_config`` y se escribe validado
    # (``validate_config``); la forma está en backend/docs/contrato-instituciones.md.
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PlatformConfig(models.Model):
    """Las configuraciones de plataforma (fase 4), con la forma de ``Organization.config``.

    - ``public``: rige ``/app``, es decir los usuarios sin institución y las
      operaciones sin institución.
    - ``template``: el portal base, sólo del superadmin. De acá sale la
      configuración de una institución nueva (fase 5).

    Sin fila, rige la configuración base del código
    (``apps.user.institution_config.BASE_CONFIG``).
    """

    class Key(models.TextChoices):
        PUBLIC = "public", _("Pública (/app)")
        TEMPLATE = "template", _("Plantilla de institución nueva")

    key = models.CharField(max_length=20, choices=Key.choices, unique=True)
    config = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class UserManager(DjangoUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("The email must be set")
        email = self.normalize_email(email)
        extra_fields.setdefault("username", email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    username = models.CharField(
        _("username"),
        max_length=150,
        unique=True,
        help_text=_(
            "Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only."
        ),
        error_messages={"unique": _("A user with that username already exists."),},
    )
    email = models.EmailField(_("email address"), unique=True)
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)
    approved = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=255, blank=True)
    role = models.CharField(
        max_length=32,
        choices=UserRole.choices,
        default=UserRole.MEMBER,
        help_text=_("Controls the default permissions assigned to a user."),
    )
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        # Borrar una institución con miembros no se puede: con SET_NULL sus
        # administradores quedaban con role=admin y sin institución, que es la
        # definición de superadmin de toda la plataforma. Primero se da de baja
        # a cada uno, a conciencia.
        on_delete=models.PROTECT,
        related_name="members",
        help_text=_("Client organization this user belongs to (e.g. CAF)."),
    )
    last_password_change = models.DateTimeField(null=True, blank=True)
    # Bloqueo por intentos fallidos de login (contraseña o MFA). Ver
    # ``apps.authentication.services.login_guard``.
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    # El último paso TOTP aceptado: un código MFA sirve una sola vez.
    mfa_last_step = models.BigIntegerField(null=True, blank=True)

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"

    def clean(self):
        super().clean()
        # Una persona de una organización sólo tiene acceso a su organización
        # (ver ``apps/user/access.py``). ``is_staff`` e ``is_superuser`` abren
        # la plataforma entera, así que no pueden convivir con una
        # organización: o es del equipo de Ecofilia, o es de un cliente.
        if self.organization_id and (self.is_staff or self.is_superuser):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                _(
                    "Un usuario de una organización no puede ser staff ni "
                    "superusuario: tendría acceso a todas las organizaciones. "
                    "Quitale la organización o los permisos de staff."
                )
            )

    def save(self, *args, **kwargs):
        # La API pudo haberle quitado en memoria el alcance global a un
        # miembro de una organización (``scope_to_organization``). Eso no se
        # persiste: se guardan los valores reales.
        unscoped = getattr(self, "_unscoped_flags", None)
        if unscoped is not None:
            scoped = (self.is_staff, self.is_superuser)
            self.is_staff, self.is_superuser = unscoped
            try:
                return self._save(*args, **kwargs)
            finally:
                self.is_staff, self.is_superuser = scoped
        return self._save(*args, **kwargs)

    def _save(self, *args, **kwargs):
        # Quien pierde su institución siendo role=admin quedaría como superadmin
        # de toda la plataforma (role=admin sin institución). Baja a member:
        # ascender a alguien a superadmin es una decisión aparte, no un efecto
        # secundario de sacarlo de su institución.
        if self.pk and self.organization_id is None and self.role == UserRole.ADMIN and not self.is_superuser:
            had_organization = (
                type(self).objects.filter(pk=self.pk, organization__isnull=False).exists()
            )
            if had_organization:
                self.role = UserRole.MEMBER
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = list({*kwargs["update_fields"], "role"})
        if self.email:
            self.username = self.email
        if self.email_verified and self.email_verified_at is None:
            self.email_verified_at = timezone.now()
        if not self.email_verified:
            self.email_verified_at = None
        return super(User, self).save(*args, **kwargs)

    @property
    def is_restricted(self) -> bool:
        """True when the user belongs to a restricted org (e.g. CAF portal)."""
        return bool(self.organization_id and self.organization.restricted)

    def mark_email_verified(self):
        self.email_verified = True
        self.email_verified_at = timezone.now()
        self.save(update_fields=["email_verified", "email_verified_at"])

    def disable_email_verification(self):
        self.email_verified = False
        self.email_verified_at = None
        self.save(update_fields=["email_verified", "email_verified_at"])

    def set_password(self, raw_password):
        super().set_password(raw_password)
        self.last_password_change = timezone.now()

    def __str__(self):
        return f"{self.id}, username: {self.username}"


class AuditEvent(models.Model):
    """Registro de auditoría: quién hizo qué, sobre qué, cuándo y desde dónde.

    Append-only: no se edita ni se borra desde la aplicación ni desde el admin.
    Cubre las acciones privilegiadas (consola, configuraciones, alcance de
    skills, biblioteca pública, alta de instituciones) y la autenticación
    (logins, bloqueos, contraseñas, MFA). Se escribe con
    ``apps.user.audit.record``.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        "user.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events"
    )
    # El email queda aunque se borre el usuario: el registro no puede perder el quién.
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=80, db_index=True)
    organization = models.ForeignKey(
        Organization, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_events",
        help_text="Institución afectada, si la hay.",
    )
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=255, blank=True)
    data = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("organization", "created_at"))]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PermissionError("El registro de auditoría no se modifica.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("El registro de auditoría no se borra.")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_email or '—'} {self.action}"
