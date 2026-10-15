"""
Qué puede hacer cada rol sobre una operación compartida.

- Quien tiene acceso —también un lector— abre sus propias conversaciones.
- Un editor trabaja la operación pero no la elimina: eso es del dueño.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.chat.models import ChatMessage, ChatSession, MessageRole
from apps.project.models import Project, ProjectShareRole

User = get_user_model()


class OperationRolesTestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="x")
        self.editor = User.objects.create_user(email="editor@example.com", password="x")
        self.viewer = User.objects.create_user(email="viewer@example.com", password="x")
        self.stranger = User.objects.create_user(email="stranger@example.com", password="x")
        self.project = Project.objects.create(owner=self.owner, name="Operación")
        self.project.shares.create(user=self.editor, role=ProjectShareRole.EDITOR)
        self.project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)
        self.sessions_url = reverse("project-chat-sessions", kwargs={"slug": self.project.slug})

    def _open_conversation(self, user):
        self.client.force_authenticate(user)
        return self.client.post(self.sessions_url, {"title": "Mi conversación"}, format="json")

    def test_everyone_with_access_opens_their_own_conversation(self):
        for user in (self.owner, self.editor, self.viewer):
            response = self._open_conversation(user)
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, (user.email, response.data))
            self.assertEqual(ChatSession.objects.get(pk=response.data["id"]).owner, user)

    def test_conversations_are_private_to_each_person(self):
        mine = ChatSession.objects.get(pk=self._open_conversation(self.viewer).data["id"])
        ChatMessage.objects.create(session=mine, role=MessageRole.USER, content="hola")
        theirs = ChatSession.objects.get(pk=self._open_conversation(self.editor).data["id"])
        ChatMessage.objects.create(session=theirs, role=MessageRole.USER, content="hola")

        self.client.force_authenticate(self.viewer)
        listed = {s["id"] for s in self.client.get(self.sessions_url).data}
        self.assertEqual(listed, {mine.id})

    def test_without_access_there_is_no_conversation(self):
        response = self._open_conversation(self.stranger)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_viewer_still_cannot_edit(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.patch(f"/api/projects/{self.project.slug}/", {"name": "otro"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_editor_cannot_delete_the_operation(self):
        self.client.force_authenticate(self.editor)
        detail = self.client.get(f"/api/projects/{self.project.slug}/").data
        self.assertTrue(detail["can_edit"])
        self.assertFalse(detail["can_delete"])
        response = self.client.delete(f"/api/projects/{self.project.slug}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_owner_deletes_it(self):
        self.client.force_authenticate(self.owner)
        self.assertTrue(self.client.get(f"/api/projects/{self.project.slug}/").data["can_delete"])
        response = self.client.delete(f"/api/projects/{self.project.slug}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
