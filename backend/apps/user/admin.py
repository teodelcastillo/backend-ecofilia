from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import Organization, User


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
                    "mfa_enabled",
                    "mfa_secret",
                    "last_password_change",
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
    readonly_fields = ("email_verified_at", "last_password_change")


admin.site.register(User, UserAdmin)
