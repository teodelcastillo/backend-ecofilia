"""
Con quién se comparte: la organización es un límite, y el error no revela si
un email tiene cuenta (ver ``apps.user.sharing``).
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.evaluation.models import Evaluation
from apps.project.models import Project
from apps.repository.models import Repository
from apps.user.models import Organization
from apps.user.sharing import SHARE_TARGET_UNAVAILABLE

User = get_user_model()


class ShareTargetTestCase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self.other_org = Organization.objects.create(name="Otro cliente", slug="otro")
        self.owner = User.objects.create_user(email="owner@caf.com", password="x", organization=self.caf)
        self.colleague = User.objects.create_user(email="colega@caf.com", password="x", organization=self.caf)
        self.other_client = User.objects.create_user(email="x@otro.com", password="x", organization=self.other_org)
        self.no_org = User.objects.create_user(email="libre@example.com", password="x")
        self.staff = User.objects.create_user(email="equipo@ecofilia.site", password="x", is_staff=True)
        self.project = Project.objects.create(owner=self.owner, name="Operación CAF")

    def _share_project(self, as_user, email, project=None):
        self.client.force_authenticate(as_user)
        url = reverse("project-shares", kwargs={"slug": (project or self.project).slug})
        return self.client.post(url, {"user_email": email, "role": "viewer"}, format="json")

    def test_member_shares_with_colleague(self):
        response = self._share_project(self.owner, self.colleague.email)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_member_shares_with_ecofilia_staff(self):
        response = self._share_project(self.owner, self.staff.email)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_member_cannot_share_outside_the_organization(self):
        for email in (self.other_client.email, self.no_org.email):
            response = self._share_project(self.owner, email)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, email)
            self.assertEqual(str(response.data["user_email"][0]), SHARE_TARGET_UNAVAILABLE)
        self.assertFalse(self.project.shares.exists())

    def test_unknown_email_gets_the_same_answer(self):
        response = self._share_project(self.owner, "nadie@otro.com")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(str(response.data["user_email"][0]), SHARE_TARGET_UNAVAILABLE)

    def test_inactive_colleague_gets_the_same_answer(self):
        self.colleague.is_active = False
        self.colleague.save()
        response = self._share_project(self.owner, self.colleague.email)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(str(response.data["user_email"][0]), SHARE_TARGET_UNAVAILABLE)

    def test_outsider_cannot_push_into_the_organization(self):
        project = Project.objects.create(owner=self.no_org, name="Proyecto ajeno")
        response = self._share_project(self.no_org, self.colleague.email, project=project)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_users_without_organization_keep_sharing_among_themselves(self):
        other = User.objects.create_user(email="otro-libre@example.com", password="x")
        project = Project.objects.create(owner=self.no_org, name="Proyecto")
        response = self._share_project(self.no_org, other.email, project=project)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_staff_shares_an_operation_with_the_client(self):
        project = Project.objects.create(owner=self.staff, name="Armada por Ecofilia")
        response = self._share_project(self.staff, self.owner.email, project=project)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_same_rule_for_documents_repositories_and_evaluations(self):
        document = Document.objects.create(owner=self.owner, name="Doc", slug="doc-caf")
        repository = Repository.objects.create(owner=self.owner, name="Repo")
        evaluation = Evaluation.objects.create(owner=self.owner, title="Eval")
        urls = [
            reverse("document-shares", kwargs={"slug": document.slug}),
            reverse("repository-shares", kwargs={"slug": repository.slug}),
            reverse("evaluation-shares", kwargs={"slug": evaluation.slug}),
        ]
        self.client.force_authenticate(self.owner)
        for url in urls:
            rejected = self.client.post(url, {"user_email": self.other_client.email, "role": "viewer"}, format="json")
            self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST, url)
            self.assertEqual(str(rejected.data["user_email"][0]), SHARE_TARGET_UNAVAILABLE, url)
            accepted = self.client.post(url, {"user_email": self.colleague.email, "role": "viewer"}, format="json")
            self.assertEqual(accepted.status_code, status.HTTP_201_CREATED, (url, accepted.data))
