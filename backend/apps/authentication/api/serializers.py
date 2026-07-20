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
        )


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
        data = super().validate(attrs)
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
                raise AuthenticationFailed(detail=_("Código MFA inválido"), code="mfa_invalid")

        logger.info("Login success for user=%s", user.pk)
        data["user"] = ProfileSerializer(user).data
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

