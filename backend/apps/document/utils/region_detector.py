"""
Automatic country / region detection for documents.

Used during document ingestion to populate ``Document.region`` without
requiring manual input from the user. Detection is regex-based (no LLM call),
using word-boundary matches against a curated list of country name variants in
Spanish, English and Portuguese.

Detection strategy (in order):
1. Normalize the document name (strip extension, underscores, hyphens → spaces).
2. Scan the normalized name for country patterns.
3. If not found, scan the first ``DETECT_CHARS`` characters of extracted text.
4. Return the canonical country name on first match, or ``None``.

Design notes
------------
- Patterns are ordered from most-specific to least-specific so that multi-word
  country names ("Costa Rica", "El Salvador") are checked before their
  component words ("Rica", "Salvador").
- All matching is case-insensitive.
- Only the first match is returned; ambiguity is handled by pattern ordering.
- This function never raises; callers should treat ``None`` as "unknown".
"""
from __future__ import annotations

import os
import re
from typing import Optional

# How many characters from the start of the extracted text to search.
DETECT_CHARS: int = int(os.environ.get("REGION_DETECT_CHARS", "10000"))


# ---------------------------------------------------------------------------
# Country patterns
# Each entry is (regex_pattern_without_word_boundaries, canonical_name).
# Listed longest/most-specific first to avoid partial matches.
# ---------------------------------------------------------------------------

_RAW_PATTERNS: list[tuple[str, str]] = [
    # ── Multi-word country names (check before their component words) ───────
    ("rep[uú]blica dominicana|dominican republic", "República Dominicana"),
    ("trinidad y tobago|trinidad and tobago", "Trinidad y Tobago"),
    ("antigua y barbuda|antigua and barbuda", "Antigua y Barbuda"),
    ("san vicente y las granadinas|saint vincent and the grenadines", "San Vicente y las Granadinas"),
    ("san crist[oó]bal y nieves|saint kitts and nevis", "San Cristóbal y Nieves"),
    ("papua nueva guinea|papua new guinea", "Papua Nueva Guinea"),
    ("guinea ecuatorial|equatorial guinea", "Guinea Ecuatorial"),
    ("guinea[- ]bis[aá]u|guinea[- ]bissau", "Guinea-Bisáu"),
    ("nueva zelanda|new zealand", "Nueva Zelanda"),
    ("reino unido|united kingdom", "Reino Unido"),
    # Sin "usa" suelto: con IGNORECASE matcheaba el verbo ("se usa para…")
    # y mandaba a Estados Unidos documentos en castellano.
    ("estados unidos|united states|ee\\.? ?uu\\.?", "Estados Unidos"),
    ("corea del norte|north korea", "Corea del Norte"),
    ("corea del sur|south korea", "Corea del Sur"),
    ("arabia saudita|saudi arabia", "Arabia Saudita"),
    ("emiratos [aá]rabes|united arab emirates", "Emiratos Árabes"),
    ("sudamérica|south america|am[eé]rica del sur", "Sudamérica"),
    ("am[eé]rica latina|latinoam[eé]rica|latin america", "América Latina"),
    ("costa rica", "Costa Rica"),
    ("el salvador", "El Salvador"),
    ("sierra leona|sierra leone", "Sierra Leona"),
    ("burkina faso", "Burkina Faso"),
    ("cabo verde|cape verde", "Cabo Verde"),
    ("santo tomé y príncipe|sao tome and principe", "Santo Tomé y Príncipe"),

    # ── Latin American cities / sub-national → national mapping ─────────────
    # Must appear before single-word country patterns so multi-word names
    # like "Buenos Aires" are matched before bare "argentina" is tried.
    ("buenos aires", "Argentina"),
    ("bogot[aá]", "Colombia"),
    ("montevideo", "Uruguay"),
    ("asunci[oó]n", "Paraguay"),

    # ── Single-word Latin American countries ─────────────────────────────────
    ("argentina|argentino", "Argentina"),
    ("bolivia|bolivian", "Bolivia"),
    ("brasil|brazil|brasileiro", "Brasil"),
    ("chile|chileno", "Chile"),
    ("colombia|colombian", "Colombia"),
    ("cuba|cubano", "Cuba"),
    ("ecuador|ecuatoriano", "Ecuador"),
    ("guatemala|guatemalteco", "Guatemala"),
    ("guyana|guyanese", "Guyana"),
    ("hait[ií]|haiti", "Haití"),
    ("honduras|hondureño", "Honduras"),
    ("jamaica|jamaican", "Jamaica"),
    ("m[eé]xico|mexico|mexicano", "México"),
    ("nicaragua|nicaragüense", "Nicaragua"),
    ("panam[aá]|panama|panameño", "Panamá"),
    ("paraguay|paraguayo", "Paraguay"),
    ("per[uú]|peru|peruano", "Perú"),
    ("surinam[e]?|surinamés", "Surinam"),
    ("uruguay|uruguayo", "Uruguay"),
    ("venezuela|venezolano", "Venezuela"),
    ("barbados", "Barbados"),
    ("belice|belize", "Belice"),
    ("bahamas", "Bahamas"),
    ("granada|grenada", "Grenada"),
    ("guadelope|martinica", "Guadalupe"),

    # ── Rest of the world (common) ────────────────────────────────────────────
    ("afganist[aá]n|afghanistan", "Afganistán"),
    ("albania", "Albania"),
    ("alemania|germany|deutsch", "Alemania"),
    ("angola", "Angola"),
    ("argelia|algeria", "Argelia"),
    ("armenia", "Armenia"),
    ("australia", "Australia"),
    ("austria", "Austria"),
    ("azerbaiy[aá]n|azerbaijan", "Azerbaiyán"),
    ("bangladesh", "Bangladesh"),
    ("bielorrusia|belarus", "Bielorrusia"),
    ("b[eé]lgica|belgium", "Bélgica"),
    ("benin|bén[ií]n", "Benín"),
    ("burundi", "Burundi"),
    ("butan|bhutan", "Bután"),
    ("botswana", "Botsuana"),
    ("camboya|cambodia", "Camboya"),
    ("camer[uú]n|cameroon", "Camerún"),
    ("canad[aá]|canada", "Canadá"),
    ("chad", "Chad"),
    ("china|chino", "China"),
    ("chipre|cyprus", "Chipre"),
    ("comoras|comoros", "Comoras"),
    ("congo", "Congo"),
    ("costa de marfil|ivory coast|c[oô]te d.ivoire", "Costa de Marfil"),
    ("croacia|croatia", "Croacia"),
    ("dinamarca|denmark", "Dinamarca"),
    ("yibuti|djibouti", "Yibuti"),
    ("egipto|egypt", "Egipto"),
    ("eritrea", "Eritrea"),
    ("eslovenia|slovenia", "Eslovenia"),
    ("espa[nñ]a|spain|español", "España"),
    ("estonia", "Estonia"),
    ("etiopía|etiopia|ethiopia", "Etiopía"),
    ("filipinas|philippines", "Filipinas"),
    ("finlandia|finland", "Finlandia"),
    ("fiyi|fiji", "Fiyi"),
    ("francia|france|franc[eé]s", "Francia"),
    ("gabón|gabon", "Gabón"),
    ("gambia", "Gambia"),
    ("georgia", "Georgia"),
    ("ghana", "Ghana"),
    ("grecia|greece", "Grecia"),
    ("guinea", "Guinea"),
    ("india|indio|indiano", "India"),
    ("indonesia", "Indonesia"),
    ("ir[aá]n|iran", "Irán"),
    ("irak|iraq", "Irak"),
    ("hungría|hungary|hungarian", "Hungría"),
    ("irlanda|ireland", "Irlanda"),
    ("islandia|iceland", "Islandia"),
    ("islas marshall|marshall islands", "Islas Marshall"),
    ("israel", "Israel"),
    ("italia|italy|italiano", "Italia"),
    ("jap[oó]n|japan|japon[eé]s", "Japón"),
    ("jordania|jordan", "Jordania"),
    ("kazajist[aá]n|kazajstan|kazakhstan|kazakh", "Kazajistán"),
    ("kenia|kenya", "Kenia"),
    ("kirguist[aá]n|kyrgyzstan", "Kirguistán"),
    ("kiribati", "Kiribati"),
    ("kuwait", "Kuwait"),
    ("laos", "Laos"),
    ("liechtenstein", "Liechtenstein"),
    ("lesoto|lesotho", "Lesoto"),
    ("l[ií]bano|lebanon", "Líbano"),
    ("liberia", "Liberia"),
    ("libia|libya", "Libia"),
    ("lituania|lithuania", "Lituania"),
    ("luxemburgo|luxembourg", "Luxemburgo"),
    ("madagascar", "Madagascar"),
    ("malasia|malaysia", "Malasia"),
    ("malaui|malawi", "Malaui"),
    ("maldivas|maldives", "Maldivas"),
    ("mali|malí", "Malí"),
    ("malta", "Malta"),
    ("marruecos|morocco", "Marruecos"),
    ("mauritania", "Mauritania"),
    ("mauricio|mauritius", "Mauricio"),
    ("micronesia", "Micronesia"),
    ("moldova", "Moldova"),
    ("mongolia", "Mongolia"),
    ("montenegro", "Montenegro"),
    ("mozambique", "Mozambique"),
    ("myanmar|birmania|burma", "Myanmar"),
    ("namibia", "Namibia"),
    ("nepal", "Nepal"),
    ("nicaragua", "Nicaragua"),
    ("n[ií]ger|niger", "Níger"),
    ("nigeria", "Nigeria"),
    ("noruega|norway", "Noruega"),
    ("nueva zelanda|new zealand", "Nueva Zelanda"),
    ("om[aá]n|oman", "Omán"),
    ("pakist[aá]n|pakistan", "Pakistán"),
    ("palestina|palestine", "Palestina"),
    ("pap[uú]a", "Papúa Nueva Guinea"),
    ("pa[ií]ses bajos|netherlands|holanda|holland", "Países Bajos"),
    ("pol[oa]nd|polonia|polaco", "Polonia"),
    ("portugal|portugu[eé]s|portuguese", "Portugal"),
    ("qatar|katar", "Qatar"),
    ("ruanda|rwanda", "Ruanda"),
    ("ruman[ií]a|romania", "Rumanía"),
    ("rusia|russia|ruso|russian federation|russian", "Rusia"),
    ("saint lucia|santa luc[ií]a", "Santa Lucía"),
    ("samoa", "Samoa"),
    ("senegal", "Senegal"),
    ("serbia", "Serbia"),
    ("singapur|singapore", "Singapur"),
    ("siria|syria|syrian arab republic|syrian", "Siria"),
    ("sri lanka", "Sri Lanka"),
    ("somalia", "Somalia"),
    ("sudáfrica|sudafrica|south africa", "Sudáfrica"),
    ("swazilandia|eswatini", "Esuatini"),
    ("suecia|sweden", "Suecia"),
    ("suiza|switzerland", "Suiza"),
    ("tayikist[aá]n|tajikistan", "Tayikistán"),
    ("tailandia|thailand", "Tailandia"),
    ("timor|timor[- ]leste", "Timor Oriental"),
    ("togo", "Togo"),
    ("tonga", "Tonga"),
    ("t[uú]nez|tunisia", "Túnez"),
    ("turkmenist[aá]n|turkmenistan", "Turkmenistán"),
    ("turqu[ií]a|turkey|türkiye", "Turquía"),
    ("tuvalu", "Tuvalu"),
    ("ucrania|ukraine", "Ucrania"),
    ("uganda", "Uganda"),
    ("uzbekist[aá]n|uzbekistan", "Uzbekistán"),
    ("vanuatu", "Vanuatu"),
    ("vietnam|viet nam", "Vietnam"),
    ("yemen", "Yemen"),
    ("zambia", "Zambia"),
    ("zimbabue|zimbabwe", "Zimbabue"),
]

# Regiones supranacionales: sólo cuentan si no aparece ningún país. CAF se
# llama "Banco de Desarrollo de América Latina y el Caribe", así que casi
# cualquier documento suyo la menciona en la primera página — y antes, por
# estar primera en la lista, le ganaba al país del documento.
_REGIONS = {"Sudamérica", "América Latina"}

# Compile patterns once at module load time.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:" + pat + r")\b", re.IGNORECASE | re.UNICODE), canonical)
    for pat, canonical in _RAW_PATTERNS
]
# Siglas que sólo valen en mayúsculas.
_CASE_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bUSA\b"), "Estados Unidos"),
]


def _normalize_name(name: str) -> str:
    """Strip file extension, replace separators with spaces, lowercase."""
    # Remove common extensions
    name = re.sub(r"\.[a-zA-Z0-9]{2,5}$", "", name)
    # Replace separators
    name = re.sub(r"[_\-]+", " ", name)
    return name.strip()


def detect_country_region(doc_name: str, extracted_text: str) -> Optional[str]:
    """
    Attempt to detect the country or region that a document is about.

    Searches in this order:
    1. Normalized document name (fastest — usually contains the country for NDCs).
    2. First ``DETECT_CHARS`` characters of extracted text.

    Returns the canonical country name (e.g. ``"Argentina"``) on the first
    match, or ``None`` if no country is detected.

    Safe to call with empty / ``None`` inputs — always returns ``None`` then.
    """
    candidates = []

    if doc_name:
        candidates.append(_normalize_name(doc_name))

    if extracted_text:
        candidates.append(extracted_text[:DETECT_CHARS])

    region_fallback: Optional[str] = None
    for text in candidates:
        if not text:
            continue
        found = _best_match(text)
        if found and found not in _REGIONS:
            return found
        region_fallback = region_fallback or found

    return region_fallback


def _best_match(text: str) -> Optional[str]:
    """El país que más aparece en ``text``; a igualdad, el que aparece antes.

    Antes ganaba el primero de la lista de patrones que apareciera en
    cualquier parte, así que una NDC de Chile que nombraba a Estados Unidos
    en la introducción quedaba como de Estados Unidos. Los patrones se siguen
    recorriendo en orden, pero cada coincidencia se tapa en el texto: "Guinea
    Ecuatorial" no cuenta además como "Guinea", ni "Costa Rica" como otra cosa.
    """
    remaining = text
    stats: dict[str, list[int]] = {}  # canónico → [apariciones, primera posición]
    for pattern, canonical in [*_PATTERNS, *_CASE_SENSITIVE_PATTERNS]:
        matches = list(pattern.finditer(remaining))
        if not matches:
            continue
        entry = stats.setdefault(canonical, [0, len(text)])
        entry[0] += len(matches)
        entry[1] = min(entry[1], matches[0].start())
        for match in reversed(matches):
            remaining = (
                remaining[: match.start()] + " " * (match.end() - match.start()) + remaining[match.end():]
            )

    countries = {k: v for k, v in stats.items() if k not in _REGIONS}
    pool = countries or stats
    if not pool:
        return None
    return min(pool, key=lambda k: (-pool[k][0], pool[k][1]))
