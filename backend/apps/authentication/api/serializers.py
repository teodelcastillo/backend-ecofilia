import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.validators import UniqueValidator
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken, TokenError

from apps.authentication.services.mfa import MFAService

User = get_user_model()
logger = logging.getLogger(__name__)


class RegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(
        validators=[
            UniqueValidator(
                queryset=User.objects.all(),
                message=_("A user with this email already exists."),
            )
        ]
    )
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("email", "password", "first_name", "last_name")

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class ProfileSerializer(serializers.ModelSerializer):
    organization = serializers.SerializerMethodField()
    # Los dos alcances de "admin" (ver ``apps/user/access.py``), resueltos acá
    # para que el frontend no los deduzca de ``role``/``is_superuser``:
    # ``is_ecofilia_admin`` toca lo que es de toda la plataforma;
    # ``is_organization_admin`` manda dentro de su institución.
    is_ecofilia_admin = serializers.SerializerMethodField()
    is_organization_admin = serializers.SerializerMethodField()
    # A dónde va al entrar (fase 3): la consola, el portal de su institución o
    # el portal público. Lo decide el back para que el front no lo deduzca.
    home_path = serializers.SerializerMethodField()
    # Superadmin o staff sin MFA: hasta activarlo, la API lo trata como un
    # usuario común (``ADMIN_REQUIRE_MFA``). El front le pide que lo active.
    mfa_required = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "role",
            "is_superuser",
            "email_verified",
            "approved",
            "mfa_enabled",
            "organization",
            "is_ecofilia_admin",
            "is_organization_admin",
            "home_path",
            "mfa_required",
        )
        read_only_fields = (
            "id",
            "email",
            "role",
            "is_superuser",
            "email_verified",
            "approved",
            "mfa_enabled",
            "organization",
            "is_ecofilia_admin",
            "is_organization_admin",
            "home_path",
            "mfa_required",
        )

    def get_mfa_required(self, obj) -> bool:
        from django.conf import settings

        from apps.user.access import is_privileged

        if not getattr(settings, "ADMIN_REQUIRE_MFA", True) or obj.mfa_enabled:
            return False
        if getattr(obj, "_privileged_without_mfa", False):
            return True
        return is_privileged(obj)

    def get_home_path(self, obj) -> str:
        from apps.user.access import home_path

        return home_path(obj)

    def get_is_ecofilia_admin(self, obj) -> bool:
        from apps.user.access import is_ecofilia_admin

        return is_ecofilia_admin(obj)

    def get_is_organization_admin(self, obj) -> bool:
        from apps.user.access import is_organization_admin

        return is_organization_admin(obj)

    def get_organization(self, obj):
        org = obj.organization
        if not org:
            return None
        return {
            "slug": org.slug,
            "name": org.name,
            "restricted": org.restricted,
            "enabled_features": org.enabled_features or [],
            "enabled_skills": list(
                org.enabled_skills.values_list("slug", flat=True)
            ),
        }


class VerifyEmailSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField()

    def validate(self, attrs):
        user = self.context["request"].user
        if not user.check_password(attrs["old_password"]):
            raise serializers.ValidationError({"old_password": _("Contraseña incorrecta")})
        return attrs

    def validate_new_password(self, value):
        validate_password(value, self.context["request"].user)
        return value


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField()

    def validate_new_password(self, value):
        validate_password(value)
        return value


class TokenObtainPairWithMFASerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        from apps.authentication.services import login_guard
        from apps.user import audit

        email = attrs.get(self.username_field)
        candidate = User.objects.filter(email__iexact=email).first() if email else None
        # Bloqueada: el mismo error que una contraseña equivocada, para no
        # confirmar que la cuenta existe.
        request = self.context.get("request")
        if candidate is not None and login_guard.is_locked(candidate):
            logger.warning("Login blocked for user=%s reason=locked", candidate.pk)
            audit.record("auth.login.failure", request=request, actor=candidate, data={"reason": "locked"})
            raise AuthenticationFailed(
                self.error_messages["no_active_account"], code="no_active_account"
            )
        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            audit.record(
                "auth.login.failure",
                request=request,
                actor=candidate,
                data={"reason": "credentials", "email": str(email or "")[:254]},
            )
            if candidate is not None and login_guard.register_failure(candidate):
                logger.warning("Account locked user=%s reason=too_many_failures", candidate.pk)
                audit.record("auth.account.locked", request=request, actor=candidate)
            raise
        user = self.user
        if not user.email_verified:
            logger.warning("Login blocked for user=%s reason=email_not_verified", user.pk)
            raise AuthenticationFailed(detail=_("Email no verificado"), code="email_not_verified")

        otp = self.context["request"].data.get("otp") if self.context.get("request") else None
        if user.mfa_enabled:
            if not otp:
                logger.info("MFA challenge required for user=%s", user.pk)
                raise AuthenticationFailed(detail=_("Ingresa el código MFA"), code="mfa_required")
            if not MFAService.verify(user, otp):
                logger.warning("Invalid MFA attempt for user=%s", user.pk)
                audit.record("auth.login.failure", request=request, actor=user, data={"reason": "mfa"})
                if login_guard.register_failure(user):
                    logger.warning("Account locked user=%s reason=too_many_mfa_failures", user.pk)
                    audit.record("auth.account.locked", request=request, actor=user)
                raise AuthenticationFailed(detail=_("Código MFA inválido"), code="mfa_invalid")

        login_guard.register_success(user)
        audit.record("auth.login.success", request=request, actor=user, data={"mfa": bool(user.mfa_enabled)})

        logger.info("Login success for user=%s", user.pk)
        # El perfil que recibe el front es el que va a ver la API: un superadmin
        # sin MFA llega como usuario común y con ``mfa_required``.
        from apps.user.access import require_mfa_for_privileges, scope_to_organization

        data["user"] = ProfileSerializer(require_mfa_for_privileges(scope_to_organization(user))).data
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def validate(self, attrs):
        self.refresh = attrs["refresh"]
        return attrs

    def save(self, **kwargs):
        try:
            RefreshToken(self.refresh).blacklist()
        except TokenError as exc:
            raise serializers.ValidationError({"refresh": _("Token inválido")}) from exc


class MFASetupSerializer(serializers.Serializer):
    secret = serializers.CharField(read_only=True)
    otpauth_url = serializers.CharField(read_only=True)


class MFAVerifySerializer(serializers.Serializer):
    code = serializers.CharField()


class MFADisableSerializer(serializers.Serializer):
    code = serializers.CharField()

