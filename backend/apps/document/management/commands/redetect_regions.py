"""
Vuelve a detectar el país de los documentos que el detector viejo clasificó mal.

El detector anterior tomaba el primer patrón de su lista que apareciera en
cualquier parte del texto: los documentos de CAF quedaban como "América Latina"
(está en el nombre del banco) y los que decían "se usa" como "Estados Unidos".
Con eso la operación de ese país no encontraba su NDC, NAP ni LTS.

Por defecto sólo mira esas regiones sospechosas y **no escribe nada**: el país
de un documento también lo puede haber cargado una persona, y este comando no
tiene cómo distinguirlo. Revisar la salida y aplicar con ``--apply``.

Uso
---
python manage.py redetect_regions                       # sospechosos, dry-run
python manage.py redetect_regions --apply               # sospechosos, escribe
python manage.py redetect_regions --all                 # todos, dry-run
python manage.py redetect_regions --slugs ndc-peru,ido  # puntuales
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.document.models import Document
from apps.document.utils.region_detector import detect_country_region

SUSPECT_REGIONS = ["América Latina", "Sudamérica", "Estados Unidos"]


class Command(BaseCommand):
    help = "Recalcula el país de documentos mal clasificados por el detector anterior."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", default=False)
        parser.add_argument("--all", action="store_true", default=False)
        parser.add_argument("--slugs", default="")

    def handle(self, *args, **options):
        qs = Document.objects.exclude(extracted_text="").only("id", "slug", "name", "region")
        slugs = [s.strip() for s in options["slugs"].split(",") if s.strip()]
        if slugs:
            qs = qs.filter(slug__in=slugs)
        elif not options["all"]:
            qs = qs.filter(region__in=SUSPECT_REGIONS)

        changed = 0
        total = 0
        for doc in qs.iterator(chunk_size=200):
            total += 1
            text = Document.objects.filter(pk=doc.pk).values_list("extracted_text", flat=True)[0]
            detected = detect_country_region(doc.name or "", text or "")
            if not detected or detected == doc.region:
                continue
            changed += 1
            self.stdout.write(f"  {doc.slug}: {doc.region!r} → {detected!r}")
            if options["apply"]:
                Document.objects.filter(pk=doc.pk).update(region=detected)

        verb = "cambiaron" if options["apply"] else "cambiarían"
        self.stdout.write(f"\n{changed} de {total} documentos {verb} de país.")
        if changed and not options["apply"]:
            self.stdout.write("Dry-run: no se escribió nada. Repetí con --apply para aplicar.")
