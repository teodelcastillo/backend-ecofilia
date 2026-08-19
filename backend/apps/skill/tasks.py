from __future__ import annotations

try:
    from celery import shared_task
except ImportError:
    shared_task = None

from apps.skill.services import execute_skill
from apps.skill.models import SkillExecution


def run_skill_sync(execution_id: int):
    execution = SkillExecution.objects.get(pk=execution_id)
    execute_skill(execution)


if shared_task:

    @shared_task(name="skill.run")
    def run_skill_task(execution_id: int):
        run_skill_sync(execution_id)

    @shared_task(name="skill.reap_stalled_executions")
    def reap_stalled_executions_task():
        from apps.skill.reliability import reap_stalled_executions

        return reap_stalled_executions()

else:

    class _SyncSkillTask:
        def delay(self, execution_id: int):
            return run_skill_sync(execution_id)

        __call__ = delay

    run_skill_task = _SyncSkillTask()
