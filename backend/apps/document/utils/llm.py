"""
Provider-agnostic LLM layer (Phase 2).

Goal: let the generation/reasoning model move to Claude **without touching the
embeddings layer** (which stays on OpenAI in ``client_openia.py``) and without
changing the dozens of call-sites that already import
``generate_chat_completion``.

How it works:
- Routing is by **model id**. ``client_openia.generate_chat_completion`` (and
  its streaming sibling) check the resolved model: a ``claude-*`` id is
  dispatched here (Anthropic); anything else stays on the OpenAI path. So any
  caller that ends up with a Claude model id — including the eval harness via
  ``RAG_EVAL_ANSWER_MODEL`` — transparently talks to Claude.
- **Model tiers** map a *role* to a model so a single ``LLM_PROVIDER=anthropic``
  flips the machinery to the right Claude tier:
    - FAST     → Haiku   (routing, reranking, internal machinery)
    - BALANCED → Sonnet  (chat answers, default generation)
    - DEEP     → Opus    (complex multi-step synthesis)
  Defaults stay on OpenAI (``MODEL_COMPLETION``) so production behaviour does
  not change until ``LLM_PROVIDER=anthropic`` (or an explicit per-tier env /
  Claude model id) is configured.

Correctness notes for the Anthropic Messages API (vs OpenAI chat):
- ``system`` is a top-level parameter, not a role inside ``messages``. We lift
  every ``system`` message out and concatenate them.
- ``max_tokens`` is **required** — we default it when the caller passes None.
- ``temperature`` is **removed** on Opus 4.x / Fable (400 if sent); we only send
  it for models that still accept it (Sonnet / Haiku).
- Prompt caching: a ``cache_control`` breakpoint is placed on the system block
  so the (large, reused) RAG context prefix is cached across a session's turns.

Not handled here (intentionally / follow-ups):
- Embeddings — Anthropic has no embeddings API; keep OpenAI for ``embed_text``.
- Tool-use loop (``generate_with_tools``) — Anthropic's tool protocol differs;
  that path stays OpenAI-only for now and guards against Claude ids.
- Native citations & structured outputs — tracked as Phase 2 follow-ups.
"""
from __future__ import annotations

import logging
import os
from typing import Generator, List, Tuple

logger = logging.getLogger(__name__)


# --- Model tiers ------------------------------------------------------------

ROLE_FAST = "fast"          # routing, reranking, chunk-context — high volume / cheap
ROLE_BALANCED = "balanced"  # chat answers, default generation
ROLE_DEEP = "deep"          # complex multi-step synthesis

_ANTHROPIC_TIER_DEFAULTS = {
    ROLE_FAST: "claude-haiku-4-5",
    ROLE_BALANCED: "claude-sonnet-5",
    ROLE_DEEP: "claude-opus-5",
}


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "openai").strip().lower()


def resolve_model(role: str) -> str:
    """Resolve a tier/role to a concrete model id.

    Precedence: explicit per-tier env (``LLM_MODEL_FAST`` / ``_BALANCED`` /
    ``_DEEP``) → provider default → OpenAI ``MODEL_COMPLETION`` fallback.
    """
    explicit = os.environ.get(f"LLM_MODEL_{role.upper()}")
    if explicit:
        return explicit
    if _provider() == "anthropic":
        return _ANTHROPIC_TIER_DEFAULTS.get(role, _ANTHROPIC_TIER_DEFAULTS[ROLE_BALANCED])
    # OpenAI / default: preserve current behaviour (one model for every tier).
    return os.environ.get("MODEL_COMPLETION", "gpt-4o-mini")


def is_anthropic_model(model: str | None) -> bool:
    return bool(model) and str(model).lower().startswith(("claude", "anthropic."))


def effective_chat_model(stored: str | None, role: str = ROLE_BALANCED) -> str:
    """Model to actually use for plain chat/answer generation.

    Sessions freeze ``model`` at creation time, so flipping ``LLM_PROVIDER``
    would otherwise never reach sessions that already exist. Resolution rules:
    - an explicit Claude id is honored (deliberate per-session override);
    - under ``LLM_PROVIDER=anthropic`` a legacy OpenAI id is upgraded to the
      given tier, so existing sessions follow the provider switch;
    - under the OpenAI provider the stored id is preserved (no behaviour change).

    ``role`` lets a caller ask for a tier other than the balanced default —
    multi-step workflows resolve to DEEP, which is what that tier is for.
    """
    if is_anthropic_model(stored):
        return stored
    if _provider() == "anthropic":
        return resolve_model(role)
    return stored or resolve_model(role)


def _anthropic_tools_enabled() -> bool:
    return os.environ.get("LLM_TOOLS_ANTHROPIC", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def tool_capable_model(stored: str | None, role: str = ROLE_BALANCED) -> str:
    """Model for tool-use paths (copilot / agentic skills).

    The Anthropic tool loop is implemented (``anthropic_chat_with_tools``), so
    by default this resolves exactly like ``effective_chat_model`` — tool paths
    follow the provider switch too. Set ``LLM_TOOLS_ANTHROPIC=0`` to pin tool
    paths back to an OpenAI model (escape hatch if the ported loop misbehaves).
    """
    if _anthropic_tools_enabled():
        return effective_chat_model(stored, role)
    if not stored or is_anthropic_model(stored):
        return os.environ.get("MODEL_COMPLETION", "gpt-4o-mini")
    return stored


# --- Anthropic client + request shaping -------------------------------------

_anthropic_singleton = None


def _anthropic_client():
    global _anthropic_singleton
    if _anthropic_singleton is None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "The 'anthropic' package is required to use Claude models. "
                "Add it to dependencies (pyproject.toml) and install it."
            ) from exc
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set but a Claude model was requested."
            )
        _anthropic_singleton = anthropic.Anthropic(api_key=api_key, max_retries=2)
    return _anthropic_singleton


def _model_accepts_temperature(model: str) -> bool:
    """Opus 4.x and Fable reject temperature (400). Sonnet/Haiku still accept it."""
    m = (model or "").lower()
    return not ("opus" in m or "fable" in m)


def _thinking_enabled() -> bool:
    return os.environ.get("LLM_THINKING", "0").strip().lower() in ("1", "true", "yes", "on")


def _prompt_caching_enabled() -> bool:
    return os.environ.get("LLM_PROMPT_CACHING", "1").strip().lower() in ("1", "true", "yes", "on")


def _default_max_tokens() -> int:
    try:
        return int(os.environ.get("LLM_MAX_TOKENS", "4096"))
    except ValueError:
        return 4096


def _build_request(
    messages: List[dict],
    *,
    model: str,
    temperature: float | None,
    max_tokens: int | None,
) -> dict:
    """Translate OpenAI-style messages into Anthropic Messages API params."""
    system_parts = [
        str(m.get("content") or "")
        for m in messages
        if m.get("role") == "system"
    ]
    system_text = "\n\n".join(p for p in system_parts if p.strip()).strip()

    convo = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    if not convo:
        # Degenerate case (system-only): fold it into a user turn.
        convo = [{"role": "user", "content": system_text or " "}]
        system_text = ""

    params: dict = {
        "model": model,
        "max_tokens": max_tokens or _default_max_tokens(),
        "messages": convo,
    }
    if system_text:
        block: dict = {"type": "text", "text": system_text}
        if _prompt_caching_enabled():
            block["cache_control"] = {"type": "ephemeral"}
        params["system"] = [block]
    if temperature is not None and _model_accepts_temperature(model):
        params["temperature"] = temperature
    if _thinking_enabled():
        params["thinking"] = {"type": "adaptive"}
    return params


# --- Completions ------------------------------------------------------------


def anthropic_chat_completion(
    messages: List[dict],
    *,
    model: str,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> Tuple[str, dict]:
    """Anthropic Messages API call shaped like ``generate_chat_completion``.

    Returns ``(text, usage)`` where ``usage`` has ``input_tokens`` /
    ``output_tokens`` / ``total_tokens`` so existing callers don't change.
    """
    client = _anthropic_client()
    params = _build_request(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    caller = client.with_options(timeout=timeout) if timeout else client
    response = caller.messages.create(**params)

    text = "".join(
        getattr(b, "text", "")
        for b in response.content
        if getattr(b, "type", None) == "text"
    ).strip()

    usage = _usage_dict(getattr(response, "usage", None))
    if not text:
        raise ValueError("Anthropic API returned an empty response")
    return text, usage


def anthropic_chat_completion_stream(
    messages: List[dict],
    *,
    model: str,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> Generator[str, None, None]:
    """Streaming variant: yields text deltas, mirroring the OpenAI stream."""
    client = _anthropic_client()
    params = _build_request(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    caller = client.with_options(timeout=timeout) if timeout else client
    with caller.messages.stream(**params) as stream:
        for text in stream.text_stream:
            if text:
                yield text


def _usage_dict(usage_obj) -> dict:
    if usage_obj is None:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    in_tok = (getattr(usage_obj, "input_tokens", 0) or 0)
    in_tok += (getattr(usage_obj, "cache_read_input_tokens", 0) or 0)
    in_tok += (getattr(usage_obj, "cache_creation_input_tokens", 0) or 0)
    out_tok = getattr(usage_obj, "output_tokens", 0) or 0
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
    }


# --- Tool use (agentic loop) --------------------------------------------------


def _to_anthropic_tools(tools: List[dict]) -> List[dict]:
    """Convert OpenAI function-calling tool defs to the Anthropic shape.

    OpenAI: ``{"type": "function", "function": {"name", "description", "parameters"}}``
    Anthropic: ``{"name", "description", "input_schema"}``
    Already-Anthropic-shaped dicts pass through unchanged.
    """
    converted: List[dict] = []
    for tool in tools or []:
        fn = tool.get("function") or {}
        converted.append(
            {
                "name": fn.get("name") or tool.get("name"),
                "description": fn.get("description") or tool.get("description", ""),
                "input_schema": (
                    fn.get("parameters")
                    or tool.get("input_schema")
                    or {"type": "object", "properties": {}}
                ),
            }
        )
    return converted


def _response_text(response) -> str:
    return "".join(
        getattr(b, "text", "")
        for b in response.content
        if getattr(b, "type", None) == "text"
    ).strip()


def anthropic_chat_with_tools(
    messages: List[dict],
    *,
    tools: List[dict],
    tool_executor,  # Callable[[str, str], str]
    model: str,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    max_iterations: int = 6,
    timeout: float | None = None,
) -> Tuple[str, dict]:
    """Anthropic-native agentic tool loop, mirroring ``generate_with_tools``.

    Accepts OpenAI-format tool definitions (converted on the fly) and the same
    ``tool_executor(name, args_json) -> str`` contract, so skill/copilot
    call-sites stay provider-agnostic.

    Loop: call → if ``stop_reason == "tool_use"`` execute every tool_use block,
    append the assistant turn verbatim plus one tool_result per tool_use, and
    call again — until the model stops or ``max_iterations`` is reached. The
    forced final call keeps ``tools`` (required when the conversation contains
    tool_use blocks) but sets ``tool_choice: none``.
    """
    import json as _json

    client = _anthropic_client()
    params = _build_request(
        messages, model=model, temperature=temperature, max_tokens=max_tokens
    )
    params["tools"] = _to_anthropic_tools(tools)
    convo = params["messages"]

    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    caller = client.with_options(timeout=timeout) if timeout else client

    def _call():
        response = caller.messages.create(**params)
        usage = _usage_dict(getattr(response, "usage", None))
        for key in total_usage:
            total_usage[key] += usage.get(key, 0)
        return response

    for _ in range(max_iterations):
        response = _call()

        if response.stop_reason != "tool_use":
            text = _response_text(response)
            if not text:
                raise ValueError("Anthropic API returned an empty response during tool loop.")
            return text, total_usage

        # Echo the assistant turn verbatim (preserves tool_use/thinking blocks),
        # then answer every tool_use with exactly one tool_result.
        convo.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            try:
                result = tool_executor(block.name, _json.dumps(block.input or {}))
            except Exception as exc:  # tool bugs shouldn't kill the loop
                logger.warning("Tool '%s' failed: %s", block.name, exc)
                result = f"Error executing tool: {exc}"
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                }
            )
        convo.append({"role": "user", "content": results})

    # Max iterations — force a final text answer. tools must stay (the convo
    # contains tool_use blocks); tool_choice none blocks further tool calls.
    params["tool_choice"] = {"type": "none"}
    response = _call()
    text = _response_text(response)
    if not text:
        raise ValueError("Anthropic API returned an empty response after max tool iterations.")
    return text, total_usage
