"""
Cifrado en reposo de los secretos MFA (``User.mfa_secret``).

Con el secreto TOTP de alguien se generan sus códigos: guardarlo en claro hacía
que cualquier lectura de la base (un backup, un volcado, una consulta de
soporte) anulara el segundo factor de todos.

- Fernet (AES-128-CBC + HMAC-SHA256), con el prefijo ``enc1:`` para distinguir lo
  cifrado de lo viejo en claro, que se sigue leyendo hasta que la migración
  ``user/0014`` lo cifra.
- Claves: ``MFA_ENCRYPTION_KEYS``, claves Fernet separadas por coma. La primera
  cifra; todas descifran, así que para rotar se agrega la nueva adelante y se
  deja la vieja hasta re-cifrar. Sin la variable, la clave se deriva de
  ``SECRET_KEY``: funciona, pero ata los secretos MFA a esa clave (rotarla deja
  sin MFA a todos). En producción conviene definirla en Secrets Manager.
"""
from __future__ import annotations

import base64
import hashlib

from django.conf import settings

PREFIX = "enc1:"


def _keys() -> list[bytes]:
    configured = [k.strip() for k in getattr(settings, "MFA_ENCRYPTION_KEYS", []) if k.strip()]
    if configured:
        return [k.encode() for k in configured]
    derived = hashlib.sha256(b"ecofilia-mfa-secret:" + settings.SECRET_KEY.encode()).digest()
    return [base64.urlsafe_b64encode(derived)]


def _box():
    from cryptography.fernet import Fernet, MultiFernet

    return MultiFernet([Fernet(key) for key in _keys()])


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return PREFIX + _box().encrypt(plain.encode()).decode()


def decrypt(stored: str) -> str:
    """El secreto en claro. Lo viejo sin cifrar se devuelve tal cual."""
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored
    return _box().decrypt(stored[len(PREFIX):].encode()).decode()


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(PREFIX)
