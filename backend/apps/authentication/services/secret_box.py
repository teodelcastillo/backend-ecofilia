"""
Cifrado en reposo de los secretos MFA (``User.mfa_secret``).

Con el secreto TOTP de alguien se generan sus códigos: guardarlo en claro hacía
que cualquier lectura de la base (un backup, un volcado, una consulta de
soporte) anulara el segundo factor de todos.

- Fernet (AES-128-CBC + HMAC-SHA256), con el prefijo ``enc1:`` para distinguir lo
  cifrado de lo viejo en claro, que se sigue leyendo hasta que la migración
  ``user/0015`` lo cifra.
- Claves: ``MFA_ENCRYPTION_KEYS``, claves Fernet separadas por coma. La primera
  cifra; todas descifran, así que para rotar se agrega la nueva adelante y se
  deja la vieja hasta re-cifrar (``manage.py recifrar_secretos_mfa``).
- Sin la variable, la clave se deriva de ``SECRET_KEY``. Es lo que rige hoy
  (el MFA no es obligatorio): funciona, pero ata los secretos a esa clave, y
  rotar ``SECRET_KEY`` deja sin MFA a quien lo activó.
- La derivada siempre descifra, aunque la variable esté definida: así, definirla
  más adelante no rompe nada. Después de definirla, ``recifrar_secretos_mfa``
  pasa todo a la clave nueva.
"""
from __future__ import annotations

import base64
import hashlib

from django.conf import settings

PREFIX = "enc1:"


def _keys() -> list[bytes]:
    """La primera cifra; todas descifran. La derivada de ``SECRET_KEY`` va al final."""
    configured = [k.strip().encode() for k in getattr(settings, "MFA_ENCRYPTION_KEYS", []) if k.strip()]
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(b"ecofilia-mfa-secret:" + settings.SECRET_KEY.encode()).digest()
    )
    return configured + [k for k in [derived] if k not in configured]


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


def rotate(stored: str) -> str:
    """``stored`` re-cifrado con la clave principal (lo viejo en claro, cifrado)."""
    if not stored:
        return ""
    if not is_encrypted(stored):
        return encrypt(stored)
    return PREFIX + _box().rotate(stored[len(PREFIX):].encode()).decode()
