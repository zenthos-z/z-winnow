"""T-D3: Multi-provider model abstraction.

Unified model factory that creates BaseChatModel instances from Settings.
Supports Anthropic and DeepSeek providers, with configuration from
environment variables.
"""

from __future__ import annotations

from typing import Any

from langchain.chat_models import init_chat_model

from z_winnow.config.settings import get_settings

# Provider constants
ANTHROPIC = "anthropic"
DEEPSEEK = "deepseek"
OPENAI = "openai"  # generic OpenAI-compatible endpoint (OpenAI/Gemini/OpenRouter/SiliconFlow/…)
VALID_PROVIDERS = frozenset({ANTHROPIC, DEEPSEEK, OPENAI})


def create_model(
    provider: str = ANTHROPIC,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    max_retries: int = 2,
    streaming: bool = False,
    **overrides: Any,
):
    """Create a BaseChatModel instance for the given provider.

    Model IDs are read from Settings (which loads from environment).
    Additional kwargs are passed through to init_chat_model().

    Args:
        provider: 'anthropic' or 'deepseek'
        temperature: Model temperature (None = use provider default)
        max_tokens: Max tokens for generation
        timeout: Request timeout in seconds
        max_retries: Max retry attempts on failure
        streaming: Enable streaming mode
        **overrides: Additional model_kwargs passed to init_chat_model().
                     Use ``model_name`` to override the model name.

    Returns:
        BaseChatModel instance (ChatAnthropic / ChatDeepSeek etc.)

    Raises:
        ValueError: If provider is not supported or API key is missing.

    Example:
        >>> model = create_model("anthropic", temperature=0.1)
        >>> model = create_model("deepseek", temperature=0.0,
        ...                      model_name="deepseek-coder")
    """
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"Unknown provider '{provider}'. Supported: {sorted(VALID_PROVIDERS)}")

    settings = get_settings()

    # Resolve model name: explicit override > Settings default
    model_name = overrides.pop("model_name", None)

    if provider == ANTHROPIC:
        model_name = model_name or settings.anthropic_model
        if not settings.anthropic_api_key_available:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file or set the environment variable."
            )
        kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "max_retries": max_retries,
            "streaming": streaming,
        }
        # Use custom base_url if configured (third-party proxy)
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url
            kwargs["model_provider"] = "openai"
            kwargs["api_key"] = settings.anthropic_api_key
            return init_chat_model(model_name, **kwargs, **overrides)
        return init_chat_model(
            model_name,
            model_provider="anthropic",
            **kwargs,
            **overrides,
        )

    elif provider == DEEPSEEK:
        # DeepSeek uses OpenAI-compatible API endpoint
        model_name = model_name or settings.deepseek_model
        if not settings.deepseek_api_key_available and not settings.openai_api_key:
            raise ValueError(
                "Neither DEEPSEEK_API_KEY nor OPENAI_API_KEY is set. "
                "DeepSeek requires one of these API keys."
            )
        api_key = settings.deepseek_api_key or settings.openai_api_key
        return init_chat_model(
            model_name,
            model_provider="openai",
            api_key=api_key,
            base_url=settings.deepseek_base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            streaming=streaming,
            **overrides,
        )

    elif provider == OPENAI:
        # Generic OpenAI-compatible path: OpenAI / Gemini / OpenRouter / SiliconFlow / …
        model_name = model_name or settings.openai_model or settings.orchestrator_model
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set. The 'openai' provider (generic "
                "OpenAI-compatible endpoint) requires it."
            )
        return init_chat_model(
            model_name,
            model_provider="openai",
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,  # empty → official OpenAI default
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            streaming=streaming,
            **overrides,
        )


def create_model_for_subagent(
    subagent: str,
    *,
    temperature: float | None = None,
    **overrides: Any,
):
    """Create a model instance for a specific subagent role.

    Uses the per-subagent model override from Settings, falling back
    to the orchestrator model.

    Args:
        subagent: One of 'unified-reporter', 'output-composer', 'topic-tracker'
        temperature: Model temperature override
        **overrides: Additional kwargs for init_chat_model()

    Returns:
        BaseChatModel instance configured for this subagent.

    Raises:
        ValueError: If subagent name is not recognized.
    """
    settings = get_settings()

    subagent_model_map = {
        "unified-reporter": settings.effective_unified_reporter_model,
        "output-composer": settings.effective_output_composer_model,
        "topic-tracker": settings.effective_topic_tracker_model,
    }

    if subagent not in subagent_model_map:
        raise ValueError(
            f"Unknown subagent '{subagent}'. Valid: {sorted(subagent_model_map.keys())}"
        )

    model_name = subagent_model_map[subagent]

    # Determine provider from model name prefix
    if model_name.startswith("claude-"):
        provider = ANTHROPIC
    elif model_name.startswith("deepseek"):
        provider = DEEPSEEK
    else:
        # Unknown prefix (gpt-/gemini-/qwen-/…) → honor the explicit orchestrator_provider
        # (defaults to anthropic, preserving prior behavior). Lets the wizard route
        # OpenAI/Gemini/compatible models through the generic openai path.
        provider = settings.orchestrator_provider

    return create_model(
        provider,
        model_name=model_name,
        temperature=temperature,
        **overrides,
    )
