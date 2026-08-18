"""
Política de validación de salida por paso.

El motor acepta esquemas de tabla con columnas tipadas y vocabularios cerrados
desde hace tiempo, pero no los hacía cumplir: un valor fuera del enum se
convertía en celda vacía, `required` se recibía y no se usaba, y un JSON
inválido degradaba el paso a texto libre. Una determinación que debía ser un
contrato terminaba siendo prosa, y dos corridas —una válida y otra inválida— se
veían como "una tiene dato y la otra no".

La rigidez es por paso porque no todos los workflows piden lo mismo, y este no
va a ser el único: una determinación auditable necesita que el contrato se
cumpla o falle; una exploración necesita poder correr sin pelear con el
validador.

**Los pasos que ya existen quedan en `lenient`** — no se les cambia el
comportamiento por una migración. El default del modelo es `strict`, así que
todo paso nuevo nace haciendo cumplir su contrato. Lo viejo sigue andando, lo
nuevo nace bien.
"""
from django.db import migrations, models


def existing_steps_stay_lenient(apps, schema_editor):
    """Congela el comportamiento actual de lo que ya está en producción."""
    SkillStep = apps.get_model("skill", "SkillStep")
    SkillStep.objects.update(output_validation="lenient")


def noop(apps, schema_editor):
    """Nada que revertir: el campo entero se va con la migración inversa."""


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0020_skillstep_evidence_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="skillstep",
            name="output_validation",
            field=models.CharField(
                choices=[
                    ("lenient", "Tolerante: normaliza lo que puede y registra el resto"),
                    ("strict", "Estricto: la salida cumple el contrato o el paso falla"),
                ],
                default="strict",
                help_text=(
                    "Qué hacer cuando la salida no cumple el esquema declarado. "
                    "'strict' hace fallar el paso; 'lenient' normaliza lo que puede y "
                    "registra el resto. Los pasos que ya existían quedaron en 'lenient' "
                    "para no cambiarles el comportamiento; los nuevos nacen estrictos."
                ),
                max_length=20,
            ),
        ),
        migrations.RunPython(existing_steps_stay_lenient, noop),
    ]
