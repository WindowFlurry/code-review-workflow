"""Chat-model construction.

Every provider-specific quirk is confined to this module, so swapping DeepSeek
for Qwen is a config change rather than a refactor.

Two DeepSeek behaviours drive the design:

1. **Thinking mode is on by default and silently ignores ``temperature``.**
   Setting ``temperature=0`` for determinism does nothing. Deterministic nodes
   (planner, verifier, summariser) must pass
   ``extra_body={"thinking": {"type": "disabled"}}`` to actually get it.

2. **Custom provider parameters must go in ``extra_body``, not
   ``model_kwargs``.** Passing them via ``model_kwargs`` produces an API error.

Roles therefore differ in how much reasoning they are allowed to spend:
reviewing benefits from thinking, structured extraction does not.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..config import Settings, get_settings
from ..errors import ConfigError

logger = logging.getLogger(__name__)

#: The pipeline's model roles. Each maps to a different reasoning configuration.
#:
#: ``coder`` and ``fixer`` belong to the homework-2 workflow (see
#: ``codereview.workflow``). They were added rather than reusing an existing
#: role so that the two projects stay separable in logs and, if the thinking
#: restriction is ever lifted, can be tuned independently.
Role = Literal["planner", "reviewer", "verifier", "qa", "reporter", "coder", "fixer"]

#: Roles whose output must be deterministic -- they emit structured JSON, and
#: creative variation there is pure downside.
_DETERMINISTIC_ROLES: frozenset[str] = frozenset({"planner", "verifier", "reporter"})


def _base_url_kwarg() -> str:
    """Return whichever base-URL keyword the installed langchain-openai expects.

    ``api_base`` was renamed to ``base_url``; which one is accepted depends on
    the installed version, and the wrong choice raises ``TypeError`` at
    construction time. Probing the model's declared fields is cheaper than
    catching that.
    """
    try:
        from langchain_deepseek import ChatDeepSeek

        fields = getattr(ChatDeepSeek, "model_fields", {})
        if "base_url" in fields:
            return "base_url"
        if "api_base" in fields:
            return "api_base"
    except Exception:  # noqa: BLE001 - probe only; fall through to the modern name
        pass
    return "base_url"


def build_chat_model(
    role: Role = "reviewer",
    settings: Settings | None = None,
    *,
    temperature: float | None = None,
    use_fallback: bool = False,
) -> Any:
    """Construct a chat model configured for *role*.

    Args:
        role: Determines thinking/reasoning-effort defaults.
        settings: Injected for tests; defaults to the process singleton.
        temperature: Explicit override. Note that DeepSeek ignores this while
            thinking is enabled -- which is why ``_DETERMINISTIC_ROLES`` disable
            thinking instead of relying on a temperature of zero.
        use_fallback: Build the secondary provider (Qwen) instead of DeepSeek.

    Raises:
        ConfigError: The required API key is missing, or the langchain extra is
            not installed.
    """
    settings = settings or get_settings()

    if use_fallback:
        return _build_fallback_model(settings, temperature=temperature)

    if not settings.has_api_key:
        raise ConfigError(
            "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and fill it in.\n"
            "If you already run Claude Code against DeepSeek "
            "(ANTHROPIC_BASE_URL=https://api.deepseek.com), the same key works here."
        )

    try:
        from langchain_deepseek import ChatDeepSeek
    except ImportError as exc:
        raise ConfigError(
            "langchain-deepseek is not installed. Run: pip install -r requirements.txt"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": settings.deepseek_model,
        "api_key": settings.deepseek_api_key,
        _base_url_kwarg(): settings.deepseek_base_url,
        "timeout": settings.llm_timeout_s,
        "max_retries": 0,  # retries are handled by middleware, so they stay observable
    }

    extra_body = _extra_body_for(settings)
    if extra_body:
        kwargs["extra_body"] = extra_body

    if role == "reviewer" and not settings.disable_thinking:
        # Only meaningful when thinking is enabled; passing it alongside
        # `thinking: disabled` would be contradictory.
        kwargs["reasoning_effort"] = settings.reasoning_effort

    if temperature is not None:
        kwargs["temperature"] = temperature

    try:
        model = ChatDeepSeek(**kwargs)
    except TypeError as exc:
        # Older/newer signature drift: retry without the optional knobs rather
        # than failing the whole run over an unsupported keyword.
        logger.warning("ChatDeepSeek rejected an optional argument (%s); retrying minimal", exc)
        minimal = {
            "model": settings.deepseek_model,
            "api_key": settings.deepseek_api_key,
        }
        model = ChatDeepSeek(**minimal)

    logger.debug("built %s model for role=%s", settings.deepseek_model, role)
    return model


def _extra_body_for(settings: Settings) -> dict[str, Any]:
    """Provider-specific request body.

    Takes no role: thinking is now disabled for every role, so there is nothing
    to vary. See the note below on why the per-role split was abandoned.

    Thinking mode is disabled for every role, not just the deterministic ones.
    That is not the original design -- the reviewer was meant to keep thinking on
    for better defect detection -- but DeepSeek's thinking mode rejects the
    forced ``tool_choice="any"`` that LangChain issues whenever an agent has a
    structured-output tool attached:

        400 - Thinking mode does not support this tool_choice

    Since ``agents/factory.py`` hardcodes ``tool_choice="any"`` there is no way
    to opt out, so the choice is thinking *or* structured output, not both.
    Structured output wins: without it the findings cannot be parsed at all, and
    raw-text parsing would be strictly worse than a deterministic schema.

    Revisit if DeepSeek lifts the restriction -- set ``DISABLE_THINKING=false``
    and the reviewer will pick up ``REASONING_EFFORT`` again.
    """
    if settings.disable_thinking:
        return {"thinking": {"type": "disabled"}}
    return {}


def _build_fallback_model(settings: Settings, *, temperature: float | None) -> Any:
    """Secondary provider, reached through its OpenAI-compatible endpoint.

    Unlike DeepSeek, DashScope's compatible mode follows the OpenAI spec, so
    ``ChatOpenAI`` is the correct client here -- the provider-specific package
    rule that applies to DeepSeek does not apply to Qwen.

    The role is not threaded through: DashScope has no equivalent of DeepSeek's
    thinking toggle, so there is nothing for it to configure.
    """
    if not settings.has_fallback:
        raise ConfigError("no fallback provider configured (set FALLBACK_MODEL and DASHSCOPE_API_KEY)")

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ConfigError("langchain-openai is required for the fallback provider") from exc

    kwargs: dict[str, Any] = {
        "model": settings.fallback_model,
        "api_key": settings.dashscope_api_key,
        "timeout": settings.llm_timeout_s,
        "max_retries": 0,
    }
    # `api_base` was renamed to `base_url`; which one is accepted depends on the
    # installed version, and the wrong choice raises TypeError at construction.
    fields = getattr(ChatOpenAI, "model_fields", {})
    kwargs["base_url" if "base_url" in fields else "api_base"] = settings.fallback_base_url

    if temperature is not None:
        kwargs["temperature"] = temperature

    logger.info("using fallback model %s", settings.fallback_model)
    return ChatOpenAI(**kwargs)


def describe_model(settings: Settings | None = None) -> str:
    """Human-readable description of the configured model, for logs and reports."""
    settings = settings or get_settings()
    if not settings.has_api_key:
        return f"{settings.deepseek_model} (no API key configured)"
    return f"{settings.deepseek_model} via {settings.deepseek_base_url}"
