"""
OpenAI Client Service

Centralized service for OpenAI API interactions across the application.
Uses a shared client instance for efficiency while allowing per-call configuration.

Architecture Decision:
- Single shared client: All apps (documents, chat, evaluations) use the same
  client instance to reuse HTTP connections and reduce overhead.
- Configuration flexibility: Model, temperature, and other parameters are
  passed per-call, allowing each app to customize behavior as needed.
- Lazy initialization: Client is created only when needed, ensuring environment
  variables are loaded.

Usage by App:
- Documents: Uses embed_text() for document chunk embeddings
- Chat: Uses generate_chat_completion() for conversational responses
- Evaluations: Uses generate_chat_completion() for structured evaluations
"""
import os
from typing import Generator, List, Tuple, Optional

import httpx
from openai import OpenAI

from apps.document.utils.llm import (
    anthropic_chat_completion,
    anthropic_chat_completion_stream,
    anthropic_chat_with_tools,
    is_anthropic_model,
)


# Model defaults from environment
MODEL_EMBEDDING = os.environ.get("MODEL_EMBEDDING", "text-embedding-3-small")
MODEL_COMPLETION = os.environ.get("MODEL_COMPLETION", "gpt-4o-mini")
# Optional dimension override (Matryoshka truncation — only supported by text-embedding-3-*)
# Leave unset to use the model's native output dimension.
# text-embedding-3-small native: 1536 | text-embedding-3-large native: 3072
# Set to 1536 when using large to keep the DB schema unchanged while getting better recall.
EMBEDDING_DIMENSIONS: int | None = (
    int(os.environ["EMBEDDING_DIMENSIONS"])
    if os.environ.get("EMBEDDING_DIMENSIONS")
    else None
)

# Lazy initialization of OpenAI client to avoid issues with env vars not being loaded
_client: Optional[OpenAI] = None


def get_openai_client() -> OpenAI:
    """
    Get or create the shared OpenAI client instance.
    
    Uses singleton pattern with lazy initialization to:
    - Ensure environment variables are loaded before client creation
    - Reuse HTTP connections across all apps (documents, chat, evaluations)
    - Reduce memory footprint and connection overhead
    
    Returns:
        OpenAI: Shared client instance
        
    Raises:
        ValueError: If OPENAI_API_KEY is not configured
    """
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY environment variable is not set. "
                "Please ensure it's configured in your .env file or environment."
            )
        _client = OpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=httpx.Timeout(connect=5.0, read=90.0, write=90.0, pool=10.0),
        )
    return _client


def generate_document_content_summary(
    *,
    title: str,
    body_text: str,
    max_input_chars: int | None = None,
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    """
    Genera un resumen corto del documento para enriquecer embeddings (chunk inicial)
    y APIs. No sustituye al campo ``description`` manual del usuario.
    """
    limit = max_input_chars or int(os.environ.get("DOCUMENT_SUMMARY_INPUT_CHARS", "14000"))
    text = (body_text or "")[:limit].strip()
    if not text:
        return ""

    title_clean = (title or "").strip() or "Sin título"
    messages = [
        {
            "role": "system",
            "content": (
                "Eres un asistente que resume documentos para un sistema de búsqueda "
                "(RAG). Escribe en español, entre 4 y 8 oraciones, sin saludos ni meta-comentarios. "
                "Incluye: de qué trata el documento, tipo de instrumento o contenido "
                "(informe, préstamo, norma, plan, etc.), país/región/sector si aparece, "
                "y los temas centrales. Sé fiel al texto; no inventes datos."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Título o nombre del archivo: {title_clean}\n\n"
                f"Extracto del documento (puede estar truncado):\n{text}"
            ),
        },
    ]
    completion, _ = generate_chat_completion(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=int(os.environ.get("DOCUMENT_SUMMARY_MAX_TOKENS", "450")),
    )
    return (completion or "").strip()


def generate_chunk_context(
    chunk_content: str,
    doc_name: str,
    doc_summary: str,
    chunk_index: int,
    *,
    section_title: str = "",
    model: str | None = None,
    max_tokens: int = 150,
) -> str:
    """
    Genera 2-3 oraciones que sitúan un chunk dentro de su documento.
    Se usa para contextual retrieval: el resultado se antepone al contenido
    del chunk tanto en el embedding como en el prompt del LLM.

    ``section_title`` (opcional): encabezado de la sección detectada por el chunker.
    Cuando está disponible, lo incluye en el prompt para dar más precisión al LLM.
    """
    content = (chunk_content or "").strip()[:1200]
    if not content:
        return ""

    title = (doc_name or "").strip() or "Sin título"
    summary = (doc_summary or "").strip()[:500] or "No disponible"
    section_hint = f"\nSección: {section_title.strip()}" if section_title.strip() else ""

    messages = [
        {
            "role": "system",
            "content": (
                "Eres un asistente especializado en análisis de documentos ESG y sostenibilidad. "
                "Tu tarea es situar un fragmento de texto dentro de su documento para mejorar "
                "la comprensión contextual en un sistema RAG. "
                "Responde ÚNICAMENTE con 2-3 oraciones concisas. Sin introducción ni explicación adicional."
            ),
        },
        {
            "role": "user",
            "content": (
                f'Documento: "{title}"{section_hint}\n'
                f"Resumen del documento: {summary}\n\n"
                f"Fragmento #{chunk_index + 1}:\n{content}\n\n"
                "Escribe 2-3 oraciones que expliquen qué sección o tema del documento representa "
                "este fragmento y qué información concreta aporta."
            ),
        },
    ]
    try:
        result, _ = generate_chat_completion(
            messages,
            model=model,
            temperature=0.1,
            max_tokens=max_tokens,
        )
        return (result or "").strip()
    except Exception:
        return ""


def embed_text(text: str, model: str | None = None) -> List[float]:
    """
    Generate embeddings for text using OpenAI's embedding model.

    Supports Matryoshka dimension truncation via the EMBEDDING_DIMENSIONS env var.
    This lets you run text-embedding-3-large at 1536 dims (same as small) for
    better recall quality without changing the DB schema.

    IMPORTANT: all chunks in the DB must use the SAME model + dimensions.
    Mixing embeddings from different models makes cosine similarity meaningless.

    Args:
        text: Text to embed.
        model: Override the embedding model (defaults to MODEL_EMBEDDING env var).

    Returns:
        List[float]: Embedding vector. Dimension depends on model + EMBEDDING_DIMENSIONS.
    """
    client = get_openai_client()
    embedding_model = model or MODEL_EMBEDDING
    kwargs: dict = {"input": text, "model": embedding_model}
    if EMBEDDING_DIMENSIONS is not None:
        kwargs["dimensions"] = EMBEDDING_DIMENSIONS
    response = client.embeddings.create(**kwargs)
    return response.data[0].embedding


def generate_chat_completion(
    messages: List[dict],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int | None = None,
    timeout: float | None = None,
    response_format: dict | None = None,
    citations_out: list | None = None,
) -> Tuple[str, dict]:
    """
    Generate chat completion using OpenAI's chat models.
    
    Used by:
    - Chat app: Conversational responses with RAG context
    - Evaluations app: Structured metric/pillar evaluations
    
    The shared client allows each app to customize behavior via parameters:
    - Chat: Uses session-specific model and temperature
    - Evaluations: Uses run-specific model and temperature
    
    Args:
        messages: List of message dicts with 'role' and 'content' keys
        model: Model to use (defaults to MODEL_COMPLETION)
        temperature: Sampling temperature (0.0-2.0, default 0.1)
        max_tokens: Maximum tokens in response (optional)
        timeout: Request timeout in seconds (optional)
        
    Returns:
        Tuple[str, dict]: (completion_text, usage_info)
            - completion_text: Generated response text
            - usage_info: Dict with 'input_tokens', 'output_tokens', 'total_tokens'
            
    Raises:
        ValueError: If API returns empty response
    """
    effective_model = model or MODEL_COMPLETION

    # Phase 2: route Claude model ids to the Anthropic provider (embeddings and
    # everything else stay on OpenAI). ``response_format`` (OpenAI JSON mode) is
    # not forwarded — Anthropic uses output_config.format; structured outputs is
    # a Phase 2 follow-up and callers already parse JSON defensively.
    if is_anthropic_model(effective_model):
        return anthropic_chat_completion(
            messages,
            model=effective_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            citations_out=citations_out,
        )

    # Format messages for OpenAI Chat Completions API
    formatted_messages = [
        {
            "role": message["role"],
            "content": message["content"],
        }
        for message in messages
    ]

    client = get_openai_client()
    
    # Build request parameters
    request_params = {
        "model": effective_model,
        "temperature": temperature,
        "messages": formatted_messages,
    }
    
    # Add optional parameters if provided
    if max_tokens is not None:
        request_params["max_tokens"] = max_tokens
    if response_format is not None:
        request_params["response_format"] = response_format
    
    # Make API call with optional timeout
    if timeout is not None:
        response = client.chat.completions.create(
            **request_params,
            timeout=timeout,
        )
    else:
        response = client.chat.completions.create(**request_params)

    # Extract the completion text from the response
    if not response.choices:
        raise ValueError("OpenAI API returned an empty response")

    raw_content = response.choices[0].message.content
    completion_text = (raw_content or "").strip()
    if not completion_text:
        raise ValueError("OpenAI API returned an empty response")

    usage_obj = getattr(response, "usage", None)
    if usage_obj is not None:
        usage = {
            "input_tokens": usage_obj.prompt_tokens or 0,
            "output_tokens": usage_obj.completion_tokens or 0,
            "total_tokens": usage_obj.total_tokens or 0,
        }
    else:
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return completion_text, usage


def generate_with_tools(
    messages: List[dict],
    *,
    tools: List[dict],
    tool_executor,  # Callable[[str, str], str]
    model: str | None = None,
    temperature: float = 0.1,
    max_iterations: int = 6,
) -> Tuple[str, dict]:
    """
    Agentic chat completion with tool-call loop.

    Calls the model, executes any requested tools, appends results to the
    conversation, and calls again — repeating until the model stops or
    ``max_iterations`` is reached.

    Args:
        messages: Initial conversation (system + user).
        tools: OpenAI-format tool definitions ([{"type":"function","function":{...}}]).
        tool_executor: Callable(tool_name, args_json_str) → result_str.
        model: Completion model (defaults to MODEL_COMPLETION).
        temperature: Sampling temperature.
        max_iterations: Hard cap on tool-call rounds to avoid infinite loops.

    Returns:
        Tuple[str, dict]: (final_text, aggregated_usage)
    """
    effective_model = model or MODEL_COMPLETION
    if is_anthropic_model(effective_model):
        # Anthropic's tool-use protocol differs from OpenAI's; the native
        # agentic loop lives in llm.py and maps results back to the same
        # (text, usage) contract callers expect.
        return anthropic_chat_with_tools(
            messages,
            tools=tools,
            tool_executor=tool_executor,
            model=effective_model,
            temperature=temperature,
            max_iterations=max_iterations,
        )
    client = get_openai_client()
    conversation = [{"role": m["role"], "content": m["content"]} for m in messages]
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=effective_model,
            temperature=temperature,
            messages=conversation,
            tools=tools,
            tool_choice="auto",
        )

        usage_obj = getattr(response, "usage", None)
        if usage_obj:
            total_usage["input_tokens"] += usage_obj.prompt_tokens or 0
            total_usage["output_tokens"] += usage_obj.completion_tokens or 0
            total_usage["total_tokens"] += usage_obj.total_tokens or 0

        if not response.choices:
            raise ValueError("OpenAI API returned an empty response during tool loop.")

        choice = response.choices[0]
        finish_reason = choice.finish_reason
        assistant_message = choice.message

        # No tool calls — model is done.
        if finish_reason != "tool_calls" or not assistant_message.tool_calls:
            content = (assistant_message.content or "").strip()
            if not content:
                raise ValueError("OpenAI API returned an empty response.")
            return content, total_usage

        # Append the assistant's tool-call turn to the conversation.
        conversation.append({
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": [tc.model_dump() for tc in assistant_message.tool_calls],
        })

        # Execute each tool and append the results.
        for tool_call in assistant_message.tool_calls:
            result = tool_executor(
                tool_call.function.name,
                tool_call.function.arguments,
            )
            conversation.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

    # Max iterations reached — do a final call without tools to force a response.
    response = client.chat.completions.create(
        model=effective_model,
        temperature=temperature,
        messages=conversation,
    )
    usage_obj = getattr(response, "usage", None)
    if usage_obj:
        total_usage["input_tokens"] += usage_obj.prompt_tokens or 0
        total_usage["output_tokens"] += usage_obj.completion_tokens or 0
        total_usage["total_tokens"] += usage_obj.total_tokens or 0

    if not response.choices:
        raise ValueError("OpenAI API returned an empty response after max tool iterations.")
    content = (response.choices[0].message.content or "").strip()
    return content, total_usage


def generate_chat_completion_stream(
    messages: List[dict],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    timeout: float | None = None,
) -> Generator[str, None, None]:
    """
    Stream chat completion tokens from OpenAI.

    Yields individual text chunks as they arrive.  The caller is responsible
    for assembling the full response and persisting the ChatMessage record.
    """
    effective_model = model or MODEL_COMPLETION
    if is_anthropic_model(effective_model):
        yield from anthropic_chat_completion_stream(
            messages,
            model=effective_model,
            temperature=temperature,
            timeout=timeout,
        )
        return

    client = get_openai_client()
    formatted_messages = [
        {"role": m["role"], "content": m["content"]}
        for m in messages
    ]
    create_kwargs: dict = {
        "model": effective_model,
        "temperature": temperature,
        "messages": formatted_messages,
        "stream": True,
    }
    if timeout is not None:
        create_kwargs["timeout"] = timeout
    stream = client.chat.completions.create(**create_kwargs)
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content