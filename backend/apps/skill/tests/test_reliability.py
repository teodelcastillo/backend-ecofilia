"""
El reaper de ejecuciones zombis.

El incidente que motiva este módulo: un worker recibió una tarea, un proceso
hijo murió por SIGKILL, y nada se enteró — la ejecución quedó `running` en la
base durante más de una hora sin que ni un proceso automático ni una persona
pudieran distinguir "sigue viva" de "murió sin avisar". Lo que se prueba acá es
exactamente esa distinción: qué se marca `stalled` y qué se deja tranquilo.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.project.models import Project
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType
from apps.skill.reliability import reap_stalled_executions

User = get_user_model()


class ReapStalledExecutionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.project = Project.objects.create(owner=self.user, name="Operación")
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )

    def _execution(self, *, status, last_progress_at=None, started_at=None, created_minutes_ago=0):
        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project, status=status,
        )
        if created_minutes_ago:
            SkillExecution.objects.filter(pk=execution.pk).update(
                created_at=timezone.now() - timedelta(minutes=created_minutes_ago)
            )
        execution.started_at = started_at
        execution.last_progress_at = last_progress_at
        execution.save(update_fields=["started_at", "last_progress_at"])
        return execution

    def test_a_running_execution_with_recent_progress_is_left_alone(self):
        execution = self._execution(
            status=ExecutionStatus.RUNNING,
            last_progress_at=timezone.now() - timedelta(minutes=2),
        )
        reaped = reap_stalled_executions(threshold_minutes=15)

        self.assertEqual(reaped, [])
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.RUNNING)

    def test_a_running_execution_stale_beyond_the_threshold_is_marked_stalled(self):
        execution = self._execution(
            status=ExecutionStatus.RUNNING,
            last_progress_at=timezone.now() - timedelta(minutes=20),
        )
        reaped = reap_stalled_executions(threshold_minutes=15)

        self.assertEqual(reaped, [execution.id])
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.STALLED)
        self.assertIn("15 minutos", execution.error_message)
        self.assertIn("reanudar", execution.error_message.lower())

    def test_completed_and_failed_executions_are_never_touched_regardless_of_age(self):
        """El umbral es sobre `running`. Una corrida terminada hace un mes no
        es un zombi — terminó, es historia."""
        for terminal_status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED):
            with self.subTest(status=terminal_status):
                execution = self._execution(
                    status=terminal_status,
                    last_progress_at=timezone.now() - timedelta(days=30),
                )
                reap_stalled_executions(threshold_minutes=15)
                execution.refresh_from_db()
                self.assertEqual(execution.status, terminal_status)

    def test_missing_last_progress_at_falls_back_to_started_at(self):
        """Ejecuciones que arrancaron antes de que existiera este campo no
        pueden quedar invisibles para siempre — necesitan una señal de
        respaldo, no un `None` que nunca compara como viejo."""
        execution = self._execution(
            status=ExecutionStatus.RUNNING,
            last_progress_at=None,
            started_at=timezone.now() - timedelta(minutes=45),
        )
        reaped = reap_stalled_executions(threshold_minutes=15)

        self.assertEqual(reaped, [execution.id])

    def test_missing_both_timestamps_falls_back_to_created_at(self):
        execution = self._execution(
            status=ExecutionStatus.RUNNING,
            last_progress_at=None,
            started_at=None,
            created_minutes_ago=45,
        )
        reaped = reap_stalled_executions(threshold_minutes=15)

        self.assertEqual(reaped, [execution.id])

    def test_a_fresh_execution_without_any_timestamp_is_not_reaped(self):
        """`created_at` es `auto_now_add`: siempre es 'ahora', así que una
        ejecución recién creada nunca cae en el umbral por la cadena de
        respaldo sola."""
        execution = self._execution(status=ExecutionStatus.RUNNING)
        reaped = reap_stalled_executions(threshold_minutes=15)

        self.assertEqual(reaped, [])
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.RUNNING)

    def test_multiple_stale_executions_are_all_reaped_in_one_pass(self):
        stale = [
            self._execution(
                status=ExecutionStatus.RUNNING,
                last_progress_at=timezone.now() - timedelta(minutes=30),
            )
            for _ in range(3)
        ]
        fresh = self._execution(
            status=ExecutionStatus.RUNNING,
            last_progress_at=timezone.now() - timedelta(minutes=1),
        )
        reaped = reap_stalled_executions(threshold_minutes=15)

        self.assertEqual(set(reaped), {e.id for e in stale})
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, ExecutionStatus.RUNNING)
