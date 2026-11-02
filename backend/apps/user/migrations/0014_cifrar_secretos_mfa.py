"""
Los secretos MFA que estaban en claro se cifran (ver
``apps/authentication/services/secret_box.py``).

Reversible: al volver, quedan otra vez en claro. Idempotente: lo ya cifrado no
se toca.
"""
from django.db import migrations


def cifrar(apps, schema_editor):
    from apps.authentication.services.secret_box import encrypt, is_encrypted

    User = apps.get_model("user", "User")
    db = schema_editor.connection.alias if schema_editor else "default"
    for user in User.objects.using(db).exclude(mfa_secret="").only("pk", "mfa_secret"):
        if not is_encrypted(user.mfa_secret):
            User.objects.using(db).filter(pk=user.pk).update(mfa_secret=encrypt(user.mfa_secret))


def descifrar(apps, schema_editor):
    from apps.authentication.services.secret_box import decrypt, is_encrypted

    User = apps.get_model("user", "User")
    db = schema_editor.connection.alias if schema_editor else "default"
    for user in User.objects.using(db).exclude(mfa_secret="").only("pk", "mfa_secret"):
        if is_encrypted(user.mfa_secret):
            User.objects.using(db).filter(pk=user.pk).update(mfa_secret=decrypt(user.mfa_secret))


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0013_institucion_protegida"),
    ]

    operations = [
        migrations.RunPython(cifrar, descifrar),
    ]
