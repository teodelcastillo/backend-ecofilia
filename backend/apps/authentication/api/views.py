import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import TokenError

from apps.authentication.api.serializers import (
    LogoutSerializer,
    MFASetupSerializer,
    MFAVerifySerializer,
    MFADisableSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ProfileSerializer,
    RegisterSerializer,
    TokenObtainPairWithMFASerializer,
    VerifyEmailSerializer,
)
from apps.authentication.services.email import EmailService
from apps.authentication.services.mfa import MFAService
from apps.authentication.services.tokens import TokenService

User = get_user_model()
logger = logging.getLogger(__name__)


def build_frontend_url(path: str, params: dict[str, str]) -> str:
    base = settings.FRONTEND_BASE_URL.rstrip("/")
    query = urlencode(params)
    if query:
        return f"{base}{path}?{query}"
    return f"{base}{path}"


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        uid = TokenService.make_uid(user)
        token = TokenService.generate_token(user)
        verification_url = build_frontend_url("/auth/verify-email", {"uid": uid, "token": token})
        try:
            EmailService.send_verification_email(user, verification_url)
        except Exception:
            logger.exception("Failed to send verification email to %s", user.email)

        return Response(
            {
                "user": ProfileSerializer(user).data,
                "detail": _("Revisa tu correo para confirmar tu cuenta."),
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = TokenService.validate_token(serializer.validated_data["uid"], serializer.validated_data["token"])
        if not user:
            return Response({"detail": _("Token inválido")}, status=status.HTTP_400_BAD_REQUEST)

        user.mark_email_verified()
        return Response({"detail": _("Email verificado")}, status=status.HTTP_200_OK)


class TokenObtainPairWithMFAView(TokenObtainPairView):
    serializer_class = TokenObtainPairWithMFASerializer
    permission_classes = [AllowAny]


class StrictRefreshThrottle(AnonRateThrottle):
    """
    Throttle específico para el endpoint de refresh token.
    Más estricto que el throttle anónimo general para prevenir ataques de fuerza bruta.
    """
    scope = "strict_refresh"  # Usa el rate configurado en DRF_THROTTLE_RATES


class CustomTokenRefreshView(TokenRefreshView):
    """
    Custom token refresh view optimized for production security.
    
    Security features:
    - Explicit rate limiting (20/min) to prevent brute force attacks
    - Ensures refresh token rotation is properly handled
    - No information disclosure in error responses
    - Proper logging without exposing sensitive data
    
    With ROTATE_REFRESH_TOKENS=True and BLACKLIST_AFTER_ROTATION=True:
    - When a refresh token is used, SimpleJWT automatically rotates it
    - The old refresh token is blacklisted
    - A new refresh token is returned in the response
    """
    serializer_class = TokenRefreshSerializer
    permission_classes = [AllowAny]
    throttle_classes = [StrictRefreshThrottle]

    def post(self, request, *args, **kwargs):
        """
        Handle token refresh with security best practices.
        
        - Validates input through serializer (handles invalid/expired tokens)
        - Ensures refresh token rotation works correctly
        - Logs security events without exposing sensitive data
        - Returns standardized responses
        """
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            
            # With ROTATE_REFRESH_TOKENS=True, the serializer should include 'refresh' in validated_data
            response_data = serializer.validated_data
            
            # Verify that refresh token is included when rotation is enabled
            # This is a safety check to ensure the frontend always gets the new refresh token
            if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS", False):
                if "refresh" not in response_data:
                    # This should never happen with proper SimpleJWT configuration
                    # Log as error for monitoring, but don't expose to client
                    logger.error(
                        "Refresh token rotation enabled but 'refresh' not in response. "
                        "Check SIMPLE_JWT configuration. User may be affected."
                    )
                    # Still return access token to avoid breaking the client
                    # The client will need to re-authenticate on next refresh attempt
            
            # Log successful refresh (without exposing tokens)
            logger.debug("Token refresh successful")
            
            return Response(response_data, status=status.HTTP_200_OK)
            
        except TokenError:
            logger.warning("Token refresh failed: TokenError (invalid/expired/blacklisted)")
            return Response(
                {"detail": _("Token is invalid or expired. Please sign in again.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except ValidationError:
            # TokenRefreshSerializer raises ValidationError for invalid/expired/blacklisted tokens
            logger.warning("Token refresh failed: ValidationError")
            return Response(
                {"detail": _("Token is invalid or expired. Please sign in again.")},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except Exception as exc:
            logger.warning(
                "Token refresh failed: %s",
                type(exc).__name__,
                exc_info=settings.DEBUG,
            )
            raise


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email__iexact=serializer.validated_data["email"]).first()
        if user:
            uid = TokenService.make_uid(user)
            token = TokenService.generate_token(user)
            reset_url = build_frontend_url("/auth/reset-password", {"uid": uid, "token": token})
            try:
                EmailService.send_password_reset_email(user, reset_url)
            except Exception:
                logger.exception("Failed to send password reset email to %s", user.email)
        return Response({"detail": _("Si el email existe recibirás instrucciones.")})


class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = TokenService.validate_token(serializer.validated_data["uid"], serializer.validated_data["token"])
        if not user:
            return Response({"detail": _("Token inválido o expirado.")}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data["new_password"])
        user.mfa_enabled = False
        user.mfa_secret = ""
        fields = ["password", "last_password_change", "mfa_enabled", "mfa_secret"]
        # El enlace llegó a este email: quien lo abrió demostró que es suyo. Sin
        # esto, alguien invitado a una institución (fase 5) nunca podría entrar.
        if not user.email_verified:
            user.email_verified = True
            fields += ["email_verified", "email_verified_at"]
        user.save(update_fields=fields)
        return Response({"detail": _("Contraseña actualizada")})


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password", "last_password_change"])
        return Response({"detail": _("Contraseña actualizada")})


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class MFASetupView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        secret = MFAService.generate_secret()
        user.mfa_secret = secret
        user.mfa_enabled = False
        user.save(update_fields=["mfa_secret", "mfa_enabled"])
        data = MFASetupSerializer({"secret": secret, "otpauth_url": MFAService.provisioning_uri(user.email, secret)}).data
        return Response(data, status=status.HTTP_201_CREATED)


class MFAVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MFAVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not MFAService.verify(user, serializer.validated_data["code"]):
            return Response({"detail": _("Código MFA inválido")}, status=status.HTTP_400_BAD_REQUEST)
        user.mfa_enabled = True
        user.save(update_fields=["mfa_enabled"])
        return Response({"detail": _("MFA activado")})


class MFADisableView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = MFADisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if user.mfa_enabled and not MFAService.verify(user, serializer.validated_data["code"]):
            return Response({"detail": _("Código MFA inválido")}, status=status.HTTP_400_BAD_REQUEST)
        user.mfa_enabled = False
        user.mfa_secret = ""
        user.save(update_fields=["mfa_enabled", "mfa_secret"])
        return Response({"detail": _("MFA desactivado")})

