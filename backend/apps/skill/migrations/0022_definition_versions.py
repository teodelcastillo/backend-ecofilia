"""
Versiones de definición, y la corrida apuntando a la suya.

Hasta acá la definición del workflow vivía editable en la base y las corridas no
guardaban a qué definición correspondían. El manifiesto anotaba una huella, que
alcanza para desconfiar —"esto cambió"— pero no para explicar qué cambió ni para
volver a la definición vieja.

**Las corridas anteriores quedan sin versión.** Rellenarlas con una "versión 1"
sintética sería afirmar que corrieron la definición de hoy: la única cosa que
este versionado existe para desmentir. Sin versión, el frente muestra
"definición no registrada", que es la verdad.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('skill', '0021_skillstep_output_validation'),
    ]

    operations = [
        migrations.CreateModel(
            name='SkillDefinitionVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version_number', models.PositiveIntegerField()),
                ('fingerprint', models.CharField(help_text='sha256 de la definición serializada. Es su identidad real.', max_length=80)),
                ('schema', models.PositiveSmallIntegerField(default=1, help_text='Versión del formato de serialización. Cambiarlo mueve todas las huellas, así que sin este campo un cambio nuestro sería indistinguible de un cambio del autor del workflow.')),
                ('definition', models.JSONField(help_text='Snapshot completo: skill, pasos y parámetros declarados.')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('skill', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='definition_versions', to='skill.skill')),
            ],
            options={
                'ordering': ('skill', '-version_number'),
            },
        ),
        migrations.AddField(
            model_name='skillexecution',
            name='definition_version',
            field=models.ForeignKey(blank=True, help_text='La definición con la que arrancó esta corrida. Vacío en las corridas anteriores al versionado: no se rellenan con una versión sintética porque afirmaría que corrieron la definición de hoy, que es exactamente la mentira que el versionado viene a evitar.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='executions', to='skill.skilldefinitionversion'),
        ),
        migrations.AddIndex(
            model_name='skilldefinitionversion',
            index=models.Index(fields=['skill', 'fingerprint'], name='skill_skill_skill_i_be6818_idx'),
        ),
        migrations.AddConstraint(
            model_name='skilldefinitionversion',
            constraint=models.UniqueConstraint(fields=('skill', 'version_number'), name='skill_definition_version_number'),
        ),
        migrations.AddConstraint(
            model_name='skilldefinitionversion',
            constraint=models.UniqueConstraint(fields=('skill', 'fingerprint'), name='skill_definition_version_fingerprint'),
        ),
    ]
