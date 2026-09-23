"""
País de un documento: es lo que usa la operación para vincular sola la NDC,
el NAP y la LTS vigentes de su país. Los casos son los que fallaban.
"""
from django.test import SimpleTestCase, TestCase

from apps.document.utils.region_detector import detect_country_region


class RegionDetectorTestCase(SimpleTestCase):
    def test_caf_name_does_not_beat_the_country(self):
        texto = (
            "CAF, banco de desarrollo de América Latina y el Caribe. "
            "La República de Colombia presenta su contribución. Colombia se compromete."
        )
        self.assertEqual(detect_country_region("NDC actualizada.pdf", texto), "Colombia")

    def test_spanish_verb_usa_is_not_the_united_states(self):
        texto = "Este documento se usa para evaluar la operación en Perú."
        self.assertEqual(detect_country_region("Informe.pdf", texto), "Perú")

    def test_the_most_mentioned_country_wins(self):
        texto = (
            "En coordinación con Estados Unidos, el Gobierno de Chile presenta la "
            "NDC de Chile. Chile reducirá sus emisiones."
        )
        self.assertEqual(detect_country_region("NDC.pdf", texto), "Chile")

    def test_uppercase_usa_still_counts(self):
        self.assertEqual(detect_country_region("Report USA 2024.pdf", ""), "Estados Unidos")

    def test_name_wins_over_text(self):
        self.assertEqual(
            detect_country_region("NDC_Ecuador_2021.pdf", "Colombia Colombia Colombia"),
            "Ecuador",
        )

    def test_region_only_when_no_country(self):
        self.assertEqual(
            detect_country_region("Estrategia.pdf", "Informe regional sobre América Latina."),
            "América Latina",
        )

    def test_region_in_the_name_yields_to_a_country_in_the_text(self):
        self.assertEqual(
            detect_country_region("America Latina - clima.pdf", "El caso de Uruguay. Uruguay."),
            "Uruguay",
        )

    def test_multiword_country_is_not_counted_twice(self):
        self.assertEqual(
            detect_country_region("Plan.pdf", "Guinea Ecuatorial y Guinea Ecuatorial; Guinea."),
            "Guinea Ecuatorial",
        )

    def test_grenada_matches_the_operation_form(self):
        self.assertEqual(detect_country_region("NDC Grenada.pdf", ""), "Grenada")

    def test_nothing_found(self):
        self.assertIsNone(detect_country_region("", "sin país"))


class RedetectRegionsCommandTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            email="r@example.com", password="x", username="r"
        )

    def _doc(self, slug, region, text):
        from apps.document.models import Document

        return Document.objects.create(
            owner=self.user, name=slug, slug=slug, region=region, extracted_text=text
        )

    def test_dry_run_reports_and_apply_writes(self):
        from io import StringIO

        from django.core.management import call_command

        doc = self._doc("ndc", "América Latina", "CAF de América Latina. Perú presenta. Perú.")
        manual = self._doc("otro", "Chile", "Texto sobre Perú.")

        out = StringIO()
        call_command("redetect_regions", stdout=out)
        doc.refresh_from_db()
        self.assertEqual(doc.region, "América Latina")
        self.assertIn("'Perú'", out.getvalue())

        call_command("redetect_regions", "--apply", stdout=StringIO())
        doc.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(doc.region, "Perú")
        # Un país que no es sospechoso no se toca sin --all.
        self.assertEqual(manual.region, "Chile")
