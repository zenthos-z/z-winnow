"""Tests for the generic OpenAI-compatible provider.

Covers the fix for the onboarding「OpenAI/Gemini/兼容端点 不生效」gap:
  - Settings: new ``openai_base_url`` / ``openai_model`` / ``orchestrator_provider``
    fields + the clamp validator (bad value → anthropic, never bricks).
  - ``create_model("openai", ...)`` routes through init_chat_model with the right
    base_url / api_key / model_provider (and None base_url → official OpenAI default).
  - ``create_model_for_subagent`` honors ``settings.orchestrator_provider`` for models
    whose name isn't ``claude-``/``deepseek`` (so OpenAI/Gemini/compatible models no
    longer misroute to the Anthropic path), while ``claude-`` keeps routing to Anthropic.
"""

from __future__ import annotations

import pytest

from z_winnow.config import reset_settings
from z_winnow.config.models import OPENAI, create_model, create_model_for_subagent
from z_winnow.config.settings import Settings

_INIT = "z_winnow.config.models.init_chat_model"


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings()
    yield
    reset_settings()


class _Stub:
    """Captures the args init_chat_model was called with; returns itself."""

    def __init__(self) -> None:
        self.captured: dict = {}

    def __call__(self, model_name: str, **kwargs):
        self.captured = {"model_name": model_name, **kwargs}
        return self


# ============================================================
# Settings defaults + clamp
# ============================================================
class TestSettingsFields:
    def test_defaults(self, monkeypatch):
        # 隔离 .env 的 provider 配置 — 测真默认值(不读 .env 文件, 删 provider env)
        monkeypatch.delenv("WINNOW_ORCHESTRATOR_PROVIDER", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_PROVIDER", raising=False)
        s = Settings(_env_file=None)
        assert s.openai_base_url == ""
        assert s.openai_model == ""
        assert s.orchestrator_provider == "deepseek"

    def test_alias_loading(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        monkeypatch.setenv("OPENAI_MODEL", "qwen3-vl-flash")
        monkeypatch.setenv("ORCHESTRATOR_PROVIDER", "OPENAI")  # uppercase → clamped lower
        s = Settings()
        assert s.openai_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert s.openai_model == "qwen3-vl-flash"
        assert s.orchestrator_provider == "openai"

    def test_bad_provider_clamps_to_deepseek(self):
        s = Settings(orchestrator_provider="garbage")
        assert s.orchestrator_provider == "deepseek"  # safe-boot, never raises


# ============================================================
# create_model("openai", ...)
# ============================================================
class TestCreateModelOpenAI:
    def test_routes_with_base_and_key(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(_INIT, stub)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv(
            "OPENAI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        create_model(OPENAI, model_name="gemini-3-flash")
        assert stub.captured["model_provider"] == "openai"
        assert stub.captured["api_key"] == "sk-test"
        assert (
            stub.captured["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        assert stub.captured["model_name"] == "gemini-3-flash"

    def test_official_default_when_no_base(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(_INIT, stub)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        create_model(OPENAI, model_name="gpt-5.4-mini")
        assert stub.captured["model_provider"] == "openai"
        assert stub.captured.get("base_url") is None  # empty → official OpenAI default

    def test_missing_key_raises(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(_INIT, stub)
        monkeypatch.setenv("OPENAI_API_KEY", "")
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            create_model(OPENAI, model_name="gpt-5.4-mini")


# ============================================================
# create_model_for_subagent routing fallback
# ============================================================
class TestSubagentRoutingFallback:
    """Patch get_settings with an explicit Settings(**kwargs) instance — explicit kwargs
    beat the local .env (which sets ORCHESTRATOR_MODEL / anthropic_base_url and would
    otherwise leak into these routing assertions)."""

    def _settings(self, monkeypatch, **kw):
        from z_winnow.config import models as models_mod

        s = Settings(**kw)
        monkeypatch.setattr(models_mod, "get_settings", lambda: s)
        return s

    def test_unknown_prefix_honors_orchestrator_provider(self, monkeypatch):
        stub = _Stub()
        monkeypatch.setattr(_INIT, stub)
        self._settings(
            monkeypatch,
            openai_api_key="sk-test",
            openai_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            orchestrator_model="gemini-3-flash",
            orchestrator_provider="openai",
        )
        create_model_for_subagent("unified-reporter")
        # gemini-3-flash isn't claude-/deepseek- → must use the openai path, NOT anthropic
        assert stub.captured["model_provider"] == "openai"
        assert stub.captured["model_name"] == "gemini-3-flash"
        assert stub.captured["base_url"].startswith("https://generativelanguage")

    def test_claude_prefix_still_uses_anthropic(self, monkeypatch):
        """Regression: claude- models keep routing to Anthropic regardless of orchestrator_provider."""
        stub = _Stub()
        monkeypatch.setattr(_INIT, stub)
        self._settings(
            monkeypatch,
            anthropic_api_key="sk-ant",
            anthropic_base_url="",  # force native anthropic branch (clear any .env base_url)
            orchestrator_model="claude-sonnet-4-20250514",
            orchestrator_provider="openai",  # prefix must still win for claude-
        )
        create_model_for_subagent("unified-reporter")
        assert stub.captured["model_provider"] == "anthropic"
