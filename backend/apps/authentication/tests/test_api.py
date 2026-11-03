import pyotp
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.authentication.services.tokens import TokenService

User = get_user_model()


class AuthenticationAPITestCase(APITestCase):
    def setUp(self):
        cache.clear()  # el throttle de anónimos vive en el caché
        self.password = "StrongPass123!"
        self.user = User.objects.create_user(
            email="user@example.com",
            username="user@example.com",
            password=self.password,
        )

    def test_register_creates_user_and_sends_email(self):
        url = reverse("auth-register")
        payload = {
            "email": "new@example.com",
            "password": "AnotherPass123!",
            "first_name": "New",
            "last_name": "User",
        }

        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email="new@example.com").exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Confirma tu email", mail.outbox[0].subject)

    def test_login_requires_email_verification(self):
        url = reverse("auth-login")
        payload = {"email": self.user.email, "password": self.password}

        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("Email no verificado", response.data["detail"])

    def test_login_succeeds_once_verified(self):
        self.user.email_verified = True
        self.user.save(update_fields=["email_verified"])

        url = reverse("auth-login")
        payload = {"email": self.user.email, "password": self.password}
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertTrue(response.data["user"]["email_verified"])

    def test_mfa_requires_otp(self):
        secret = pyotp.random_base32()
        self.user.email_verified = True
        self.user.mfa_secret = secret
        self.user.mfa_enabled = True
        self.user.save(update_fields=["email_verified", "mfa_secret", "mfa_enabled"])

        url = reverse("auth-login")
        payload = {"email": self.user.email, "password": self.password}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        payload["otp"] = pyotp.TOTP(secret).now()
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)

    def test_password_reset_flow_updates_password(self):
        self.user.email_verified = True
        self.user.save(update_fields=["email_verified"])

        request_url = reverse("auth-password-reset")
        self.client.post(request_url, {"email": self.user.email}, format="json")

        uid = TokenService.make_uid(self.user)
        token = TokenService.generate_token(self.user)
        confirm_url = reverse("auth-password-reset-confirm")
        payload = {"uid": uid, "token": token, "new_password": "BrandNewPass123!"}
        response = self.client.post(confirm_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BrandNewPass123!"))

    def test_password_change_endpoint(self):
        self.user.email_verified = True
        self.user.save(update_fields=["email_verified"])
        self.client.force_authenticate(user=self.user)

        url = reverse("auth-password-change")
        payload = {"old_password": self.password, "new_password": "EvenStronger123!"}
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("EvenStronger123!"))

    def test_register_duplicate_email_does_not_reveal_the_account(self):
        # Antes daba 400 «already exists»: probando emails se sabía quién usa
        # Ecofilia. Ahora la respuesta es la de un alta nueva, y a la cuenta
        # existente le llega un email que se lo cuenta.
        url = reverse("auth-register")
        new = self.client.post(
            url, {"email": "otro@example.com", "password": "StrongPass456!"}, format="json"
        )
        mail.outbox.clear()
        payload = {
            "email": "USER@example.com",
            "password": "StrongPass456!",
            "first_name": "Dup",
            "last_name": "User",
        }
        response = self.client.post(url, payload, format="json")

        self.assertEqual((response.status_code, response.data), (new.status_code, new.data))
        self.assertEqual(User.objects.filter(email__iexact="user@example.com").count(), 1)
        self.assertEqual([m.to for m in mail.outbox], [["user@example.com"]])
        self.assertIn("Ya tenés una cuenta", mail.outbox[0].subject)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))  # nada cambió

    def test_register_weak_password_is_rejected_whether_or_not_the_email_exists(self):
        url = reverse("auth-register")
        for email in ("user@example.com", "nadie@example.com"):
            with self.subTest(email=email):
                response = self.client.post(url, {"email": email, "password": "123"}, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(list(response.data), ["password"])

    def test_register_weak_password_returns_field_error(self):
        url = reverse("auth-register")
        payload = {
            "email": "weak@example.com",
            "password": "123",
            "first_name": "Weak",
            "last_name": "Pass",
        }
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)

    def test_register_common_password_returns_field_error(self):
        url = reverse("auth-register")
        payload = {
            "email": "common@example.com",
            "password": "password12345678",
            "first_name": "Common",
            "last_name": "Pass",
        }
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)

