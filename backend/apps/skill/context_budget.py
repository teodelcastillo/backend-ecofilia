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
        return {
            "delivery_mode": "context_first",
            "budget_tokens": self.budget_tokens,
            "reserved_tokens": self.reserved_tokens,
            "corpus_tokens": self.corpus_tokens,
            "documents_total": len(self.deliveries),
            "documents_full": sum(1 for d in self.deliveries if d.mode == FULL),
            "documents_partial": [d.slug for d in self.degraded],
            "documents_unavailable": [d.slug for d in self.unavailable],
            "scope_complete": self.complete,
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
                )
            )
        else:
            deliveries.append(
                DocumentDelivery(
                    document=document, mode=FULL, tokens=tokens, full_tokens=tokens
                )
            )

    # Degradar de mayor a menor hasta que el conjunto entre. El principal va
    # al final de la cola de candidatos, no importa cuánto pese.
    def _degrade_order(delivery: DocumentDelivery) -> tuple[int, int]:
        is_blueprint = blueprint_id is not None and delivery.document.id == blueprint_id
        return (1 if is_blueprint else 0, -delivery.full_tokens)

    candidates = sorted(
        (d for d in deliveries if d.mode == FULL), key=_degrade_order
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
    for index, delivery in enumerate(plan.deliveries, start=1):
        pages = getattr(delivery.document, "page_count", None)
        extent = f" · {pages} páginas" if pages else ""
        entry = (
            f"{index}. [{delivery.slug}] {delivery.name}{extent} — "
            f"{_STATE_LABEL.get(delivery.mode, delivery.mode)}"
        )
        if delivery.reason:
            entry += f" ({delivery.reason})"
        lines.append(entry)

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
    return "\n".join(lines)


def render_corpus(plan: ContextPlan, *, texts: dict, partial_blocks: dict) -> str:
    """El cuerpo documental, delimitado documento por documento.

    Los delimitadores llevan el slug además del nombre porque es el
    identificador con el que después se resuelve la fuente en la interfaz, y
    porque un nombre de archivo es ambiguo entre versiones del mismo documento.
    """
    total = len(plan.deliveries)
    parts: list[str] = []
    for index, delivery in enumerate(plan.deliveries, start=1):
        header = (
            f"===== DOCUMENTO {index}/{total} · [{delivery.slug}] {delivery.name} · "
            f"{_STATE_LABEL.get(delivery.mode, delivery.mode)} ====="
        )
        if delivery.mode == FULL:
            body = (texts.get(delivery.document.id) or "").strip()
        elif delivery.mode == PARTIAL:
            body = (partial_blocks.get(delivery.document.id) or "").strip()
            if not body:
                body = (
                    "(No se recuperaron fragmentos de este documento para esta "
                    "sección. No asumas nada sobre su contenido.)"
                )
        else:
            body = "(Sin texto extraído. Este documento no puede usarse como fuente.)"
        parts.append(f"{header}\n{body}\n===== FIN DOCUMENTO {index}/{total} =====")
    return "\n\n".join(parts)
