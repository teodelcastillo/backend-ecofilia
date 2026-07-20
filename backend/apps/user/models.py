from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserRole(models.TextChoices):
    ADMIN = "admin", _("Administrator")
    MANAGER = "manager", _("Manager")
    MEMBER = "member", _("Member")


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
    last_password_change = models.DateTimeField(null=True, blank=True)
    organization = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Client organization slug (e.g. 'caf'). Null = internal user."),
    )

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"

    def save(self, *args, **kwargs):
        if self.email:
            self.username = self.email
        if self.email_verified and self.email_verified_at is None:
            self.email_verified_at = timezone.now()
        if not self.email_verified:
            self.email_verified_at = None
        return super(User, self).save(*args, **kwargs)

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
