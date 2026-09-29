"""
Una corrida no condiciona a las siguientes.

Cada corrida arranca de la definición del workflow, los documentos y los datos
de la operación, y de nada que haya escrito una corrida anterior: ni en la
misma operación, ni en otra que comparta documentos, ni al repetirla. Lo único
que viaja entre pasos es el historial *de la misma corrida*, y eso es a
propósito.

El test marca la salida de la primera corrida y busca la marca en todo lo que
las siguientes le mandan al modelo. El control positivo —la marca sí aparece en
el paso 2 de la misma corrida— es lo que prueba que la búsqueda funciona: sin
él, un test que no encuentra nada no distingue aislamiento de un buscador roto.
"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill.models import SkillExecution, Skill, SkillStep, SkillType
from apps.skill.services import execute_skill, rerun_execution

User = get_user_model()

MARCA = "MARCA-DE-UNA-CORRIDA-ANTERIOR"


def _sent(mock_completion) -> list[str]:
    """Todo lo que se le mandó al modelo, una entrada por llamada."""
    return [json.dumps(call.args[0], ensure_ascii=False, default=str) for call in mock_completion.call_args_list]


class RunIsolationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="iso@example.com", password="x", username="iso")
        # Un documento compartido por dos operaciones, como los instrumentos
        # del país (la NDC, el NAP) que usan todas las de ese país.
        self.shared = Document.objects.create(
            owner=self.user,
            name="NDC",
            slug="ndc-compartida",
            extracted_text="La NDC compromete una reducción de emisiones del 51% al 2030.",
        )
        self.operation = self._operation("Operación A")
        self.other_operation = self._operation("Operación B")

        self.skill = Skill.objects.create(
            owner=self.user,
            name="IET de prueba",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="Sos un analista.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Marco", instructions="Describí el marco.", position=1
        )
        SkillStep.objects.create(
            skill=self.skill, title="Síntesis", instructions="Integrá lo anterior.", position=2
        )

    def _operation(self, name: str) -> Project:
        project = Project.objects.create(owner=self.user, name=name)
        ProjectDocument.objects.create(project=project, document=self.shared, added_by=self.user)
        return project

    def _run(self, execution: SkillExecution) -> list[str]:
        with patch("apps.skill.services.generate_chat_completion") as mock_completion:
            mock_completion.return_value = (f"{MARCA}: texto escrito por el modelo.", {"total_tokens": 1})
            execute_skill(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.status, "completed", execution.error_message)
        return _sent(mock_completion)

    def _new(self, project: Project) -> SkillExecution:
        return SkillExecution.objects.create(skill=self.skill, owner=self.user, project=project)

    def test_within_a_run_later_steps_do_see_earlier_ones(self):
        """Control positivo: si esto falla, el resto de los tests no prueba nada."""
        sent = self._run(self._new(self.operation))

        self.assertNotIn(MARCA, sent[0])
        self.assertIn(MARCA, sent[1])

    def test_a_new_run_on_the_same_operation_starts_clean(self):
        first = self._new(self.operation)
        self._run(first)
        # Aunque la anterior sea la vigente del informe.
        first.promoted_at = first.finished_at
        first.save(update_fields=["promoted_at"])

        sent = self._run(self._new(self.operation))

        self.assertNotIn(MARCA, sent[0])

    def test_a_run_on_another_operation_sharing_documents_starts_clean(self):
        self._run(self._new(self.operation))

        sent = self._run(self._new(self.other_operation))

        self.assertNotIn(MARCA, sent[0])

    def test_a_rerun_copies_the_input_not_the_output(self):
        first = self._new(self.operation)
        self._run(first)

        sent = self._run(rerun_execution(first))

        self.assertNotIn(MARCA, sent[0])
