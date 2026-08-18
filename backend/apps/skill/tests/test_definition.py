"""
La definición serializada: huella, diff, y qué campos entran.

Trabaja sobre objetos de prueba con la misma forma que ``Skill``/``SkillStep``
—igual que ``context_budget``— así que ninguno de estos tests toca la base. El
único test que necesita una skill real es el de resolución de versión, que
vive en ``test_definition_version.py``.
"""
from dataclasses import dataclass, field

from django.test import SimpleTestCase

from apps.skill import definition as defmod


@dataclass
class FakeSkill:
    skill_type: str = "copilot"
    name: str = "IET DATBC"
    description: str = ""
    system_prompt: str = "Sé preciso."
    prompt_template: str = ""
    tier: str = "balanced"
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    comparative_mode_enabled: bool = False
    strict_missing_evidence: bool = True
    retrieval_query_template: str = ""
    retrieval_strategy: str = "global"
    k_per_doc: int = 2
    total_limit: int = 12
    max_per_doc_after_rerank: int = 4
    default_output_mode: str = "text"
    table_schema: dict = field(default_factory=dict)
    pinned_document_slugs: list = field(default_factory=list)
    tools_enabled: bool = False
    research_phase_enabled: bool = False
    research_queries: list = field(default_factory=list)
    slug: str = "caf-iet"

    class _Related:
        def __init__(self, items):
            self._items = items

        def all(self):
            return list(self._items)

    def __post_init__(self):
        self._steps: list = []
        self._parameters: list = []

    @property
    def steps(self):
        return FakeSkill._Related(self._steps)

    @property
    def parameters(self):
        return FakeSkill._Related(self._parameters)


@dataclass
class FakeStep:
    position: int
    title: str = "Paso"
    instructions: str = "Instrucciones"
    step_type: str = "instruction"
    document_slugs: list = field(default_factory=list)
    tier: str = ""
    evidence_mode: str = "both"
    output_mode: str = "text"
    table_schema: dict = field(default_factory=dict)
    output_validation: str = "strict"
    approval_required: bool = False
    linked_skill: object = None


@dataclass
class FakeParameter:
    key: str
    label: str = ""
    param_type: str = "text"
    description: str = ""
    default_value: str = ""
    required: bool = False
    options: list = field(default_factory=list)
    position: int = 1


def make_skill(steps=None, parameters=None) -> FakeSkill:
    skill = FakeSkill()
    skill._steps = steps or []
    skill._parameters = parameters or []
    return skill


class SerializeDefinitionTests(SimpleTestCase):
    def test_same_definition_yields_the_same_fingerprint(self):
        skill = make_skill(steps=[FakeStep(position=1), FakeStep(position=2)])
        a = defmod.serialize_definition(skill)
        b = defmod.serialize_definition(skill)

        self.assertEqual(defmod.fingerprint(a), defmod.fingerprint(b))

    def test_changing_a_step_instruction_changes_the_fingerprint(self):
        """Es la garantía que la Fase 7 pide: un paso editado no puede pasar
        desapercibido en la huella."""
        skill_a = make_skill(steps=[FakeStep(position=1, instructions="Redactá A")])
        skill_b = make_skill(steps=[FakeStep(position=1, instructions="Redactá B")])

        fp_a = defmod.fingerprint(defmod.serialize_definition(skill_a))
        fp_b = defmod.fingerprint(defmod.serialize_definition(skill_b))
        self.assertNotEqual(fp_a, fp_b)

    def test_step_order_in_the_relation_does_not_matter(self):
        """Los pasos se ordenan por posición, no por el orden en que llegan de
        la base — si no, dos corridas idénticas podrían divergir por el orden
        de una query."""
        step_1, step_2 = FakeStep(position=1, title="Uno"), FakeStep(position=2, title="Dos")
        skill_shuffled = make_skill(steps=[step_2, step_1])
        skill_ordered = make_skill(steps=[step_1, step_2])

        fp_shuffled = defmod.fingerprint(defmod.serialize_definition(skill_shuffled))
        fp_ordered = defmod.fingerprint(defmod.serialize_definition(skill_ordered))
        self.assertEqual(fp_shuffled, fp_ordered)

    def test_output_validation_is_part_of_the_definition(self):
        """Dos corridas con distinta política de validación no son
        comparables: una tolera una celda inválida y la otra falla."""
        skill_strict = make_skill(steps=[FakeStep(position=1, output_validation="strict")])
        skill_lenient = make_skill(steps=[FakeStep(position=1, output_validation="lenient")])

        fp_strict = defmod.fingerprint(defmod.serialize_definition(skill_strict))
        fp_lenient = defmod.fingerprint(defmod.serialize_definition(skill_lenient))
        self.assertNotEqual(fp_strict, fp_lenient)

    def test_mutable_fields_are_copied_not_shared(self):
        """Si la serialización comparte la lista/dict del objeto vivo, editar el
        objeto después de serializarlo corrompería un snapshot que se guarda
        como inmutable."""
        schema = {"columns": [{"key": "criterio"}]}
        step = FakeStep(position=1, table_schema=schema)
        skill = make_skill(steps=[step])

        snapshot = defmod.serialize_definition(skill)
        schema["columns"].append({"key": "intruso"})

        self.assertEqual(len(snapshot["steps"][0]["table_schema"]["columns"]), 1)

    def test_skill_ref_step_folds_in_the_linked_skill_fingerprint(self):
        """Un paso que delega en otra skill no es estable si esa otra skill
        cambia — la huella tiene que notarlo."""
        linked_a = make_skill(steps=[FakeStep(position=1, instructions="v1")])
        linked_a.slug = "quick-a"
        linked_b = make_skill(steps=[FakeStep(position=1, instructions="v2")])
        linked_b.slug = "quick-a"  # mismo slug, otro contenido

        skill_using_a = make_skill(
            steps=[FakeStep(position=1, step_type="skill_ref", linked_skill=linked_a)]
        )
        skill_using_b = make_skill(
            steps=[FakeStep(position=1, step_type="skill_ref", linked_skill=linked_b)]
        )

        fp_a = defmod.fingerprint(defmod.serialize_definition(skill_using_a))
        fp_b = defmod.fingerprint(defmod.serialize_definition(skill_using_b))
        self.assertNotEqual(fp_a, fp_b)

    def test_every_model_field_is_declared_covered_or_ignored(self):
        """Ancla contra el bug que este módulo vino a evitar: un campo nuevo en
        el modelo que nadie decidió si entra o no en la definición."""
        from apps.skill.models import Skill, SkillParameter, SkillStep

        skill_model_fields = {f.name for f in Skill._meta.get_fields() if f.concrete}
        declared = set(defmod.SKILL_FIELDS) | set(defmod.SKILL_FIELDS_IGNORED)
        self.assertEqual(
            skill_model_fields - declared,
            set(),
            "Hay campos de Skill sin decidir si son parte de la definición.",
        )

        step_model_fields = {f.name for f in SkillStep._meta.get_fields() if f.concrete}
        declared_steps = set(defmod.STEP_FIELDS) | set(defmod.STEP_FIELDS_IGNORED)
        self.assertEqual(
            step_model_fields - declared_steps,
            set(),
            "Hay campos de SkillStep sin decidir si son parte de la definición.",
        )

        param_model_fields = {f.name for f in SkillParameter._meta.get_fields() if f.concrete}
        declared_params = set(defmod.PARAMETER_FIELDS) | set(defmod.PARAMETER_FIELDS_IGNORED)
        self.assertEqual(
            param_model_fields - declared_params,
            set(),
            "Hay campos de SkillParameter sin decidir si son parte de la definición.",
        )


class DiffDefinitionsTests(SimpleTestCase):
    def test_identical_definitions_have_no_diff(self):
        skill = make_skill(steps=[FakeStep(position=1)])
        a = defmod.serialize_definition(skill)
        b = defmod.serialize_definition(skill)
        self.assertEqual(defmod.diff_definitions(a, b), [])

    def test_changed_instruction_is_reported_by_step_position(self):
        skill_a = make_skill(steps=[FakeStep(position=1, instructions="Viejo")])
        skill_b = make_skill(steps=[FakeStep(position=1, instructions="Nuevo")])

        changes = defmod.diff_definitions(
            defmod.serialize_definition(skill_a), defmod.serialize_definition(skill_b)
        )
        paths = [c["path"] for c in changes]
        self.assertIn("steps[1].instructions", paths)

    def test_inserting_a_step_at_the_front_does_not_relabel_every_step(self):
        """Los pasos se emparejan por posición, no por índice de lista: agregar
        un paso al principio no puede leerse como "cambiaron los diecisiete"."""
        skill_a = make_skill(steps=[FakeStep(position=1, title="Original")])
        skill_b = make_skill(
            steps=[
                FakeStep(position=1, title="Nuevo primero"),
                FakeStep(position=2, title="Original"),
            ]
        )

        changes = defmod.diff_definitions(
            defmod.serialize_definition(skill_a), defmod.serialize_definition(skill_b)
        )
        kinds = {c["path"]: c["kind"] for c in changes}
        # posición 1 cambió de contenido, posición 2 es enteramente nueva.
        self.assertEqual(kinds.get("steps[2]"), "added")
        self.assertIn("steps[1].title", kinds)

    def test_removed_parameter_is_reported_by_key_not_position(self):
        skill_a = make_skill(parameters=[FakeParameter(key="marco"), FakeParameter(key="anio")])
        skill_b = make_skill(parameters=[FakeParameter(key="anio")])

        changes = defmod.diff_definitions(
            defmod.serialize_definition(skill_a), defmod.serialize_definition(skill_b)
        )
        self.assertIn({"path": "parameters[marco]", "kind": "removed"}, changes)

    def test_long_values_are_truncated_in_the_diff(self):
        long_text = "x" * 10_000
        skill_a = make_skill(steps=[FakeStep(position=1, instructions="corto")])
        skill_b = make_skill(steps=[FakeStep(position=1, instructions=long_text)])

        changes = defmod.diff_definitions(
            defmod.serialize_definition(skill_a), defmod.serialize_definition(skill_b)
        )
        entry = next(c for c in changes if c["path"] == "steps[1].instructions")
        self.assertLess(len(entry["b"]), len(long_text))
        self.assertIn("car.", entry["b"])
