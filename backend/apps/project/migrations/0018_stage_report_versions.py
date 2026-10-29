"""
Versiones del informe del ejecutivo: uno emitido puede reemplazarse por una
versión nueva sin anularlo. El vigente es el que no está anulado ni reemplazado.
"""
import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("project", "0017_stage_transitions_and_reports"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectstagereport",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="projectstagereport",
            name="previous",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="project.projectstagereport",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="projectstagereport",
            name="project_stage_report_one_active",
        ),
        migrations.AddConstraint(
            model_name="projectstagereport",
            constraint=models.UniqueConstraint(
                condition=Q(voided_at__isnull=True, superseded_at__isnull=True),
                fields=("project", "stage"),
                name="project_stage_report_one_current",
            ),
        ),
    ]
