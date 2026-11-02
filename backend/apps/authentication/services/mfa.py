import hmac
import time

from django.conf import settings
from django.db.models import Q

import pyotp


class MFAService:
    """Manages TOTP secrets and verifications."""

    @staticmethod
    def generate_secret() -> str:
        return pyotp.random_base32()

    @staticmethod
    def provisioning_uri(email: str, secret: str) -> str:
        issuer = getattr(settings, "MFA_ISSUER_NAME", "Ecofilia")
        return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)

    @staticmethod
    def verify(user, code: str) -> bool:
        """Si ``code`` es válido para ``user``, y lo consume.

        Un código sirve una sola vez: se guarda el paso TOTP aceptado y no se
        acepta ese ni uno anterior. Sin esto, un código visto (por encima del
        hombro, en un log) servía para entrar durante su ventana de 30-90 s.
        """
        if not user.mfa_secret or not code:
            return False
        totp = pyotp.TOTP(user.mfa_secret)
        now = time.time()
        code = str(code).strip()
        for offset in (0, -1, 1):
            moment = now + offset * totp.interval
            if hmac.compare_digest(totp.at(moment), code):
                step = int(moment // totp.interval)
                if user.mfa_last_step is not None and step <= user.mfa_last_step:
                    return False
                updated = type(user).objects.filter(pk=user.pk).filter(
                    Q(mfa_last_step__isnull=True) | Q(mfa_last_step__lt=step)
                ).update(mfa_last_step=step)
                user.mfa_last_step = step
                return bool(updated)
        return False

