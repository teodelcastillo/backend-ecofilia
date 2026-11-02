from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import AuditEvent, Organization, User


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "slug", "restricted", "member_count")
    list_filter = ("restricted",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("enabled_skills", "default_project_skills")

    def member_count(self, obj):
        return obj.members.count()

    member_count.short_description = _("Members")


class UserAdmin(DjUserAdmin):
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("first_name", "last_name", "username")}),
        (
            _("Security"),
            {
                "fields": (
                    "role",
                    "organization",
                    "approved",
                    "email_verified",
                    "email_verified_at",
                    # ``mfa_secret`` no se muestra: quien lo lee genera los
                    # códigos de esa persona. Para recuperar a alguien que
                    # perdió su app alcanza con apagar ``mfa_enabled``.
                    "mfa_enabled",
                    "last_password_change",
                    "failed_login_attempts",
                    "locked_until",
                )
            },
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "role", "organization", "password1", "password2"),
            },
        ),
    )
    list_display = (
        "id",
        "email",
        "organization",
        "role",
        "email_verified",
        "mfa_enabled",
        "is_active",
    )
    list_filter = ("organization", "role", "email_verified", "mfa_enabled", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)
    readonly_fields = ("email_verified_at", "last_password_change", "failed_login_attempts")


admin.site.register(User, UserAdmin)


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Sólo lectura: el registro de auditoría no se crea, edita ni borra a mano."""

    list_display = ("created_at", "actor_email", "action", "organization", "target_type", "target_id", "ip")
    list_filter = ("action", "organization")
    search_fields = ("actor_email", "action", "target_id")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
