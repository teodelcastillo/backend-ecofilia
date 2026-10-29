from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


class EmailService:

    @staticmethod
    def _send(subject: str, text_body: str, html_body: str, recipient: str) -> None:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=False)

    @classmethod
    def send_verification_email(cls, user, verification_url: str) -> None:
        first_name = user.first_name or user.email
        context = {
            "first_name": first_name,
            "verification_url": verification_url,
            "recipient_email": user.email,
        }
        html = render_to_string("emails/verification.html", context)
        text = (
            f"Hola {first_name},\n\n"
            f"Confirmá tu cuenta haciendo clic en el siguiente enlace:\n{verification_url}\n\n"
            "Si no solicitaste esta acción, ignorá este correo."
        )
        cls._send("Confirmá tu cuenta — Ecofilia", text, html, user.email)

    @classmethod
    def send_invitation_email(cls, user, organization, invite_url: str) -> None:
        """Invitación a una institución (fase 5): el enlace lleva a elegir la contraseña."""
        days = max(1, settings.PASSWORD_RESET_TIMEOUT // 86400)
        context = {
            "organization_name": organization.name,
            "invite_url": invite_url,
            "recipient_email": user.email,
            "days": days,
        }
        html = render_to_string("emails/invitation.html", context)
        text = (
            f"Hola,\n\n"
            f"Te invitaron a {organization.name} en Ecofilia. Elegí tu contraseña en este enlace "
            f"(vale por {days} días):\n{invite_url}\n\n"
            "Si no esperabas esta invitación, ignorá este correo."
        )
        cls._send(f"Te invitaron a {organization.name} — Ecofilia", text, html, user.email)

    @classmethod
    def send_password_reset_email(cls, user, reset_url: str) -> None:
        first_name = user.first_name or user.email
        context = {
            "first_name": first_name,
            "reset_url": reset_url,
            "recipient_email": user.email,
        }
        html = render_to_string("emails/password_reset.html", context)
        text = (
            f"Hola {first_name},\n\n"
            f"Restablecé tu contraseña usando el siguiente enlace:\n{reset_url}\n\n"
            "Si no solicitaste este cambio, contactá al soporte inmediatamente."
        )
        cls._send("Restablecer contraseña — Ecofilia", text, html, user.email)
