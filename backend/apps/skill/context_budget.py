"""
Qué documentos entran enteros en la ventana, y qué se hace con los que no.

Hasta ahora el motor nunca le mostraba los documentos al modelo: le mostraba
hasta doce fragmentos de ~500 tokens elegidos por una búsqueda, distintos en
cada paso y en cada corrida. Sobre la operación 34 eso fue entre el 0% y el
1,2% de lo asignado. El efecto no es que el informe quede corto: es que cuando
el modelo escribe "ninguno de los documentos del expediente menciona X", está
describiendo lo que la búsqueda no trajo, y ni el informe ni quien lo lee
pueden distinguir una cosa de la otra.

Con una ventana de un millón de tokens esa elección deja de ser necesaria. El
camino principal pasa a ser mandar los documentos completos; la recuperación
queda para lo único que no tiene otra salida: un documento que por sí solo no
entra.

Dos decisiones de diseño que conviene no perder:

**Se mide antes de llamar, no se atrapa el error.** Esperar el 400 de la API
para reaccionar significa que el modo degradado se decide después de haber
armado el pedido, sin registro de por qué, y que un cambio de tokenizador se
manifiesta como una corrida fallida. Acá el presupuesto se calcula primero y el
plan queda escrito en el manifiesto.

**Se degrada por documento, no por corrida.** Si el expediente no entra, la
respuesta no es volver a fragmentos para todo: es mandar enteros los que entran
y aplicar recuperación *dentro* del que sobra. En la operación 34 el BTR es el
54% del corpus él solo — degradándolo a él, los otros cinco viajan completos.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Relación caracteres/token del español sobre el tokenizador de la generación
# actual de Claude. Medida entre 2,42 y 2,60 sobre el corpus real de la
# operación 34; acá se usa por debajo del extremo bajo a propósito. Errar por
# exceso degrada un documento antes de lo necesario; errar por defecto rompe
# la llamada con el pedido ya armado.
CHARS_PER_TOKEN = float(os.environ.get("SKILL_CHARS_PER_TOKEN", "2.3"))

CONTEXT_WINDOW = int(os.environ.get("SKILL_CONTEXT_WINDOW", "1000000"))

# Colchón sobre la ventana. Cubre el error del estimador y lo que el pedido
# cobra por su propia estructura —encabezados de bloque, definiciones de
# herramientas, andamiaje del formato— que no está en el texto que medimos.
CONTEXT_SAFETY_MARGIN = int(os.environ.get("SKILL_CONTEXT_SAFETY_MARGIN", "60000"))

# Presupuesto que recibe un documento que no entra entero. Es bastante más que
# los doce fragmentos del esquema anterior porque ahora es el único documento
# sobre el que se busca, en vez de repartirse entre todos.
DEGRADED_DOC_TOKENS = int(os.environ.get("SKILL_DEGRADED_DOC_TOKENS", "20000"))

# Lo que hay que dejar libre para la respuesta. Es la misma variable que fija
# el tope de salida del proveedor a propósito: si se sube el tope sin bajar el
# presupuesto documental, la llamada se rompe justo en el paso más largo.
def output_reserve() -> int:
    try:
        return int(os.environ.get("LLM_MAX_TOKENS", "4096"))
    except ValueError:
        return 4096


# Tokens que ocupa en promedio un fragmento recuperado, para traducir el
# presupuesto de un documento degradado a una cantidad de fragmentos.
TOKENS_PER_CHUNK = int(os.environ.get("SKILL_TOKENS_PER_CHUNK", "500"))


def chunks_for_budget(tokens: int) -> int:
    """Cuántos fragmentos pedir para cubrir un presupuesto en tokens."""
    return max(1, tokens // max(1, TOKENS_PER_CHUNK))


FULL = "full"              # el texto completo del documento viaja al modelo
PARTIAL = "partial"        # no entra entero: van fragmentos recuperados de él
UNAVAILABLE = "unavailable"  # no hay texto extraído; no hay nada que mandar


def estimate_tokens(text: str) -> int:
    """Tokens de un texto, estimados por longitud.

    No se cuenta contra la API a propósito: contar exige subir el texto, y
    subir el corpus entero diecisiete veces por corrida solo para saber cuánto
    pesa cuesta más que el margen que ahorraría. Para decidir un presupuesto,
    una estimación deliberadamente pesimista alcanza.
    """
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass
class DocumentDelivery:
    """Cómo llega un documento al modelo en este paso."""

    document: object
    mode: str
    tokens: int          # lo que ocupa tal como se va a mandar
    full_tokens: int     # lo que ocuparía entero (para explicar la degradación)
    reason: str = ""
    # El documento principal de la operación: el que dice qué se financia. Los
    # demás son el marco contra el que se lo evalúa. No es una prioridad de
    # presupuesto, es una diferencia de naturaleza — ver `render_inventory`.
    is_blueprint: bool = False

    @property
    def slug(self) -> str:
        return getattr(self.document, "slug", "") or ""

    @property
    def name(self) -> str:
        return getattr(self.document, "name", "") or self.slug


@dataclass
class ContextPlan:
    """El reparto de la ventana para un paso."""

    deliveries: list[DocumentDelivery] = field(default_factory=list)
    budget_tokens: int = 0
    corpus_tokens: int = 0
    reserved_tokens: int = 0
    # Documentos degradados de los que no se pudo traer ni un fragmento, por
    # slug y motivo. Lo llena el runner después de intentar la recuperación.
    partial_failures: dict = field(default_factory=dict)

    @property
    def blueprint(self) -> DocumentDelivery | None:
        return next((d for d in self.deliveries if d.is_blueprint), None)

    @property
    def degraded(self) -> list[DocumentDelivery]:
        return [d for d in self.deliveries if d.mode == PARTIAL]

    @property
    def unavailable(self) -> list[DocumentDelivery]:
        return [d for d in self.deliveries if d.mode == UNAVAILABLE]

    @property
    def complete(self) -> bool:
        """Si todo documento con texto viajó entero."""
        return not self.degraded

    def diagnostics(self) -> dict:
        """Lo que se persiste por paso para poder explicar la corrida después."""
        extra = (
            {"partial_retrieval_failures": dict(self.partial_failures)}
            if self.partial_failures
            else {}
        )
        return {
            **extra,
            "delivery_mode": "context_first",
            "budget_tokens": self.budget_tokens,
            "reserved_tokens": self.reserved_tokens,
            "corpus_tokens": self.corpus_tokens,
            "documents_total": len(self.deliveries),
            "documents_full": sum(1 for d in self.deliveries if d.mode == FULL),
            "documents_partial": [d.slug for d in self.degraded],
            "documents_unavailable": [d.slug for d in self.unavailable],
            "scope_complete": self.complete,
            "blueprint": (self.blueprint.slug if self.blueprint else None),
            # Se registra aparte de `documents_partial` porque no es un
            # documento degradado más: es el sujeto de la auditoría llegando
            # incompleto. Quien lea la corrida después tiene que tropezarse
            # con esto, no deducirlo cruzando dos listas.
            "blueprint_degraded": bool(
                self.blueprint is not None and self.blueprint.mode == PARTIAL
            ),
            "chars_per_token": CHARS_PER_TOKEN,
        }


def plan_context(
    documents,
    *,
    reserved_tokens: int,
    blueprint_id: int | None = None,
    texts: dict | None = None,
) -> ContextPlan:
    """Decide, documento por documento, qué entra entero y qué se degrada.

    ``reserved_tokens`` es todo lo que ya está comprometido en el pedido —
    system prompt, instrucciones del paso, secciones previas, salida esperada—
    medido por el llamador antes de llegar acá. Restarlo es lo que hace que el
    presupuesto sea del paso y no del corpus en abstracto.

    ``blueprint_id`` es el documento principal de la operación: se degrada
    último. Es el que define de qué se trata la operación, y un informe escrito
    sin él es distinto en naturaleza a uno al que le falta un anexo.

    ``texts`` permite pasar los textos ya cargados; sin él se leen del
    documento. Es para no releer el mismo corpus una vez por paso.
    """
    budget = max(0, CONTEXT_WINDOW - CONTEXT_SAFETY_MARGIN - max(0, reserved_tokens))

    deliveries: list[DocumentDelivery] = []
    for document in documents:
        is_blueprint = blueprint_id is not None and document.id == blueprint_id
        text = (texts or {}).get(document.id) if texts else None
        if text is None:
            text = document.extracted_text or ""
        tokens = estimate_tokens(text)
        if not tokens:
            deliveries.append(
                DocumentDelivery(
                    document=document,
                    mode=UNAVAILABLE,
                    tokens=0,
                    full_tokens=0,
                    reason="el documento no tiene texto extraído",
                    is_blueprint=is_blueprint,
                )
            )
        else:
            deliveries.append(
                DocumentDelivery(
                    document=document,
                    mode=FULL,
                    tokens=tokens,
                    full_tokens=tokens,
                    is_blueprint=is_blueprint,
                )
            )

    # Degradar de mayor a menor hasta que el conjunto entre. El documento
    # principal **no es candidato**: es el sujeto de la auditoría, el que dice
    # qué se financia. Un informe escrito sobre fragmentos del resto es un
    # informe incompleto; uno escrito sobre fragmentos del principal es un
    # informe sobre otra cosa.
    candidates = sorted(
        (d for d in deliveries if d.mode == FULL and not d.is_blueprint),
        key=lambda d: -d.full_tokens,
    )

    def _total() -> int:
        return sum(d.tokens for d in deliveries)

    for delivery in candidates:
        if _total() <= budget:
            break
        delivery.mode = PARTIAL
        delivery.tokens = min(DEGRADED_DOC_TOKENS, delivery.full_tokens)
        delivery.reason = (
            f"{delivery.full_tokens:,} tokens estimados, no entra completo".replace(
                ",", "."
            )
        )
        logger.info(
            "Documento %s degradado a fragmentos: %d tokens sobre un presupuesto de %d.",
            delivery.slug,
            delivery.full_tokens,
            budget,
        )

    # Último recurso, y sólo si la física no deja opción: todo lo demás ya se
    # degradó y el conjunto sigue sin entrar. Se hace ruido a propósito — es la
    # única situación en la que el informe se escribe sin el documento que
    # define de qué trata la operación.
    blueprint = next((d for d in deliveries if d.is_blueprint), None)
    if blueprint is not None and blueprint.mode == FULL and _total() > budget:
        blueprint.mode = PARTIAL
        blueprint.tokens = min(DEGRADED_DOC_TOKENS, blueprint.full_tokens)
        blueprint.reason = (
            "no entra ni siendo el documento principal: todo el resto ya está "
            f"en fragmentos ({blueprint.full_tokens:,} tokens estimados)".replace(",", ".")
        )
        logger.error(
            "El documento principal %s se degradó a fragmentos: %d tokens sobre un "
            "presupuesto de %d. El informe se va a escribir sin el texto completo de "
            "la operación que evalúa.",
            blueprint.slug,
            blueprint.full_tokens,
            budget,
        )

    plan = ContextPlan(
        deliveries=deliveries,
        budget_tokens=budget,
        corpus_tokens=_total(),
        reserved_tokens=max(0, reserved_tokens),
    )
    if _total() > budget:
        # Todo degradado y todavía no entra. Puede pasar con muchísimos
        # documentos: cada uno aporta su cuota de fragmentos. Se registra y se
        # sigue; el pedido es más chico que la ventana por el colchón.
        logger.warning(
            "El alcance del paso no entra ni degradado: %d tokens sobre %d.",
            _total(),
            budget,
        )
    return plan


# ---------------------------------------------------------------------------
# Rendered para el prompt
# ---------------------------------------------------------------------------

_STATE_LABEL = {
    FULL: "TEXTO COMPLETO",
    PARTIAL: "FRAGMENTOS",
    UNAVAILABLE: "NO DISPONIBLE",
}


def render_inventory(plan: ContextPlan) -> str:
    """El inventario explícito de lo que el paso tiene — y de lo que no tiene.

    Es la mitad del producto. Un modelo que recibe documentos sin saber cuáles
    son no puede distinguir "esto no está en el expediente" de "esto no me
    llegó", y termina afirmando lo primero cuando lo cierto es lo segundo. El
    inventario convierte esa distinción en algo que el modelo puede consultar.
    """
    lines = ["## Base documental asignada a este paso", ""]
    if not plan.deliveries:
        lines.append("(Este paso no tiene documentos asignados.)")
        return "\n".join(lines)

    lines.append("Estos son los únicos documentos que tenés. No hay otros.")
    lines.append("")

    def _entry(index: int, delivery: DocumentDelivery) -> str:
        pages = getattr(delivery.document, "page_count", None)
        extent = f" · {pages} páginas" if pages else ""
        entry = (
            f"{index}. [{delivery.slug}] {delivery.name}{extent} — "
            f"{_STATE_LABEL.get(delivery.mode, delivery.mode)}"
        )
        if delivery.reason:
            entry += f" ({delivery.reason})"
        return entry

    # Los dos grupos no son una comodidad de presentación. Uno describe la
    # operación que se evalúa; los otros son el marco contra el que se la
    # evalúa. Aplanarlos invita a confundir "la operación no hace X" con "el
    # marco no exige X", que son hallazgos distintos y se presentan con la
    # misma seguridad.
    blueprint = plan.blueprint
    if blueprint is not None and len(plan.deliveries) > 1:
        lines.append("**Documento de la operación** — describe qué se financia:")
        lines.append(_entry(1, blueprint))
        lines.append("")
        lines.append("**Marco de referencia** — contra esto se evalúa la operación:")
        index = 2
        for delivery in plan.deliveries:
            if delivery.is_blueprint:
                continue
            lines.append(_entry(index, delivery))
            index += 1
        lines.append("")
        lines.append(
            "La distinción importa al declarar una ausencia: si algo no está en el "
            "documento de la operación, el hallazgo es **sobre la operación**; si no "
            "está en el marco, el hallazgo es **sobre el marco**. No son lo mismo y no "
            "se redactan igual."
        )
        if blueprint.mode == PARTIAL:
            lines.append("")
            lines.append(
                "**Atención: del documento de la operación recibís sólo fragmentos.** "
                "No tenés el texto completo de lo que se financia, así que no podés "
                "afirmar qué contempla o deja de contemplar la operación. Declaralo "
                "como limitación del análisis en vez de concluir sobre ella."
            )
    else:
        for index, delivery in enumerate(plan.deliveries, start=1):
            lines.append(_entry(index, delivery))

    lines.extend(["", "Reglas de uso de esta base:", ""])
    lines.append(
        "- Solo podés citar como fuente los documentos de esta lista. Citá siempre "
        "por su nombre."
    )
    lines.append(
        "- Si un documento de la lista menciona otro que no está en ella —un plan, "
        "una ley, un estudio—, podés señalar que existe y que sería relevante "
        "consultarlo, pero **nunca** lo cites como fuente ni le atribuyas contenido: "
        "no lo tenés."
    )
    lines.append("- Distinguí en tu redacción tres estados, y no los mezcles:")
    lines.append(
        "  · **afirmado con fuente** — está en un documento de la lista; nombrá cuál."
    )
    lines.append(
        "  · **mencionado, no disponible** — otro documento dice que existe, pero no "
        "forma parte de esta base."
    )
    lines.append(
        "  · **no encontrado** — lo buscaste en los documentos completos de la lista "
        "y no está."
    )
    if plan.degraded:
        degraded = ", ".join(f"[{d.slug}]" for d in plan.degraded)
        lines.append(
            f"- De {degraded} recibís **solo fragmentos**, no el texto completo. Sobre "
            "esos documentos podés afirmar lo que dicen los fragmentos, pero **nunca** "
            "afirmar que algo no está en ellos: no los viste enteros."
        )
    else:
        lines.append(
            "- Todos los documentos de la lista te llegan completos: si algo no está "
            "en ellos, podés afirmarlo."
        )

    if are_citations_enabled() and any(d.mode == FULL for d in plan.deliveries):
        lines.extend(["", "Sobre las citas:", ""])
        lines.append(
            "- Los documentos completos llegan como documentos citables. Cuando "
            "afirmes algo que sale de uno de ellos, **citá el pasaje del que sale**: "
            "la cita queda registrada con su ubicación exacta y es lo que le permite "
            "a quien revise el informe ir a verificarla."
        )
        lines.append(
            "- Citá el pasaje que sostiene la afirmación, no el párrafo entero "
            "alrededor. Una cita que abarca dos páginas no ubica nada."
        )
        if plan.degraded:
            lines.append(
                "- Los documentos que llegan en fragmentos **no** son citables: "
                "podés usarlos y nombrarlos en el texto, pero no generan una cita "
                "verificable. Decilo así cuando te apoyes en ellos."
            )
    return "\n".join(lines)


def _header(index: int, total: int, delivery: DocumentDelivery) -> str:
    """Delimitador de documento.

    Lleva el slug además del nombre porque es el identificador con el que
    después se resuelve la fuente en la interfaz, y porque un nombre de archivo
    es ambiguo entre versiones del mismo documento.
    """
    return (
        f"===== DOCUMENTO {index}/{total} · [{delivery.slug}] {delivery.name} · "
        f"{_STATE_LABEL.get(delivery.mode, delivery.mode)} ====="
    )


def render_partials(plan: ContextPlan, *, partial_blocks: dict) -> str:
    """Los fragmentos de los documentos que no entraron completos.

    Vacío cuando todo entró, que es el caso que se busca.
    """
    if not plan.degraded:
        return ""
    total = len(plan.deliveries)
    positions = {d.document.id: i for i, d in enumerate(plan.deliveries, start=1)}
    parts = [
        "## Fragmentos de los documentos que no entraron completos",
        "",
        "Lo que sigue son extractos recuperados para esta sección, no el "
        "documento. Sobre estos documentos no afirmes ausencias.",
    ]
    for delivery in plan.degraded:
        body = (partial_blocks.get(delivery.document.id) or "").strip()
        if not body:
            body = (
                "(No se recuperaron fragmentos de este documento para esta "
                "sección. No asumas nada sobre su contenido.)"
            )
        index = positions.get(delivery.document.id, 0)
        parts.append(
            f"\n{_header(index, total, delivery)}\n{body}\n"
            f"===== FIN FRAGMENTOS {index}/{total} ====="
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Documentos como bloques citables
# ---------------------------------------------------------------------------

# Citas nativas: el modelo devuelve, por afirmación, el texto literal y su
# ubicación en el documento. Es una variable porque la API las exige "todas o
# ninguna" por pedido: si algo sale mal, se apagan enteras sin desplegar.
def are_citations_enabled() -> bool:
    return os.environ.get("SKILL_CITATIONS", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class DocumentPayload:
    """Un documento tal como viaja: texto limpio y su mapa de páginas.

    El mapa se conserva junto al texto porque es lo único que traduce la
    ubicación que devuelve la API —un rango de caracteres— a la referencia que
    necesita quien lee el informe: una página.
    """

    slug: str
    name: str
    text: str
    page_map: object  # apps.document.page_map.PageMap

    @property
    def has_pages(self) -> bool:
        return bool(getattr(self.page_map, "has_pages", False))


def build_document_payloads(plan: ContextPlan, *, texts: dict) -> list[DocumentPayload]:
    """Los documentos completos del plan, listos para viajar como bloques.

    Sólo los completos: de un documento degradado no se puede emitir una cita
    verificable —los offsets de un puñado de fragmentos no son offsets del
    documento— y prefiero que no tenga bloque citable a que tenga uno que
    apunte a la página equivocada.
    """
    from apps.document.page_map import build_page_map

    payloads: list[DocumentPayload] = []
    for delivery in plan.deliveries:
        if delivery.mode != FULL:
            continue
        raw = texts.get(delivery.document.id) or ""
        page_map = build_page_map(raw)
        payloads.append(
            DocumentPayload(
                slug=delivery.slug,
                name=delivery.name,
                text=page_map.text,
                page_map=page_map,
            )
        )
    return payloads


def _document_block(payload: DocumentPayload, *, citations: bool) -> dict:
    """Un bloque ``document`` con fuente de texto plano.

    El título lleva el slug además del nombre porque es el identificador con
    el que la interfaz resuelve el documento, y porque la API lo devuelve tal
    cual en ``document_title``: es lo que permite atar una cita a una fila de
    la base sin depender del orden de los bloques.
    """
    block: dict = {
        "type": "document",
        "source": {
            "type": "text",
            "media_type": "text/plain",
            "data": payload.text,
        },
        "title": f"[{payload.slug}] {payload.name}",
    }
    if citations:
        block["citations"] = {"enabled": True}
    return block


def render_documents_inline(payloads: list[DocumentPayload]) -> str:
    """Los documentos como texto corrido, para proveedores sin bloques.

    Es la ruta de escape: correcta, sin citas y sin caché. No es el camino
    previsto.
    """
    total = len(payloads)
    parts = []
    for index, payload in enumerate(payloads, start=1):
        parts.append(
            f"===== DOCUMENTO {index}/{total} · [{payload.slug}] {payload.name} =====\n"
            f"{payload.text}\n"
            f"===== FIN DOCUMENTO {index}/{total} ====="
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Armado del pedido
# ---------------------------------------------------------------------------

# TTL del punto de caché sobre el corpus. Vacío usa el default del proveedor
# (5 minutos, que se renuevan con cada lectura). Un workflow de diecisiete
# pasos con pausas de aprobación puede superarlo; ahí conviene "1h", que cuesta
# el doble escribir y se amortiza a partir del tercer paso.
CACHE_TTL = os.environ.get("SKILL_CONTEXT_CACHE_TTL", "").strip()


def cache_control() -> dict | None:
    from apps.document.utils.llm import is_prompt_caching_enabled

    if not is_prompt_caching_enabled():
        return None
    control = {"type": "ephemeral"}
    if CACHE_TTL:
        control["ttl"] = CACHE_TTL
    return control


def build_messages(
    *,
    system_prompt: str,
    inventory: str,
    documents: list[DocumentPayload],
    corpus_volatile: str,
    step_prompt: str,
    model: str,
) -> list[dict]:
    """Arma el pedido con el corpus como prefijo cacheable.

    Vive en este módulo y no en el runner porque el orden de las partes *es*
    la decisión de presupuesto: la caché de prompts es un match de prefijo, y
    todo lo que esté antes del punto de caché se cobra una sola vez mientras no
    cambie. De ahí el orden exacto:

      1. ``inventory`` — qué documentos tiene el paso y qué puede afirmar
         sobre cada uno. Idéntico en los diecisiete pasos.
      2. ``documents`` — un bloque ``document`` por documento completo, con
         citas activadas. También idéntico en los diecisiete pasos.
         **El punto de caché va sobre el último de estos bloques.**
      3. ``corpus_volatile`` — los fragmentos de los documentos degradados, que
         dependen de la consulta del paso y por lo tanto cambian en cada uno.
      4. ``step_prompt`` — la instrucción, los parámetros y las secciones
         previas.

    Meter (3) adentro de (1)+(2) invalida la caché en cada paso: medio millón
    de tokens pagados diecisiete veces por veinte mil que cambian. Es el error
    que esta separación corrige, y por eso hay un test que compara los
    diecisiete prefijos byte a byte.

    Los documentos van como bloques ``document`` y no como texto porque es lo
    que habilita las citas nativas: la API devuelve entonces, por afirmación,
    el fragmento literal y su ubicación. Un bloque de texto corriente no lleva
    esa información, y sin ella la trazabilidad depende de que el modelo se
    acuerde de nombrar la fuente — que es exactamente lo que no se puede
    verificar.

    Si el modelo no es de Anthropic todo va inline, sin bloques, sin citas y
    sin caché: correcto pero caro. Es una ruta de escape, no el camino previsto.
    """
    from apps.document.utils.llm import is_anthropic_model

    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    if not is_anthropic_model(model):
        pieces = [
            p
            for p in (
                inventory,
                render_documents_inline(documents),
                corpus_volatile,
                step_prompt,
            )
            if p
        ]
        messages.append({"role": "user", "content": "\n\n".join(pieces)})
        return messages

    if not inventory and not documents:
        pieces = [p for p in (corpus_volatile, step_prompt) if p]
        messages.append({"role": "user", "content": "\n\n".join(pieces)})
        return messages

    citations = are_citations_enabled()
    content: list[dict] = []
    if inventory:
        content.append({"type": "text", "text": inventory})
    for payload in documents:
        content.append(_document_block(payload, citations=citations))

    # El punto de caché va sobre el último bloque estable, sea el último
    # documento o el inventario cuando no hay ninguno completo.
    control = cache_control()
    if control and content:
        content[-1]["cache_control"] = control

    if corpus_volatile:
        content.append({"type": "text", "text": corpus_volatile})
    content.append({"type": "text", "text": step_prompt})
    messages.append({"role": "user", "content": content})
    return messages
