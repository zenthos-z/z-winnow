"""T-W12-8: Tests for unified mock mode switch matrix.

Verifies:
  - B1: Single Settings field group controls global mock/real behavior
  - B2: Old env var name (WINNOW_REAL_LLM) still works but emits DeprecationWarning
  - R1: Settings fields control real mock/real switching behavior

P012: Each test self-contained with monkeypatch isolation + cleanup.
A013: All reads via get_settings() at call time (no module-level frozen values).
"""

from __future__ import annotations

import inspect
import warnings

import pytest

from z_winnow.config.settings import Settings, get_settings, reset_settings

# ============================================================
# Fixtures — P012: monkeypatch isolation
# ============================================================


@pytest.fixture(autouse=True)
def _clean_settings():
    """Reset Settings singleton before and after each test (P012)."""
    reset_settings()
    yield
    reset_settings()


def _clean_mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all mock-related env vars for clean test isolation."""
    for key in [
        "WINNOW_ENV",
        "ENVIRONMENT",
        "WINNOW_MOCK_LLM",
        "MOCK_LLM",
        "WINNOW_MOCK_MEMOS",
        "MOCK_MEMOS",
        "WINNOW_REAL_LLM",
    ]:
        monkeypatch.delenv(key, raising=False)


# ============================================================
# B1: Default values — production mode, no mocks
# ============================================================


class TestMockControlDefaults:
    """B1: Verify default production mode (no mocks)."""

    def test_environment_default_production(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        s = Settings()
        assert s.environment == "production"

    def test_mock_llm_default_false(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        s = Settings()
        assert s.mock_llm is False

    def test_mock_memos_default_false(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        s = Settings()
        assert s.mock_memos is False


# ============================================================
# B1: Per-service overrides work independently
# ============================================================


class TestPerServiceOverrides:
    """B1: Per-service mock flags override environment."""

    def test_mock_llm_true_in_production(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_MOCK_LLM", "true")
        s = Settings()
        assert s.use_mock_llm is True
        assert s.use_mock_memos is False  # Not affected

    def test_mock_memos_true_in_production(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_MOCK_MEMOS", "true")
        s = Settings()
        assert s.use_mock_memos is True
        assert s.use_mock_llm is False  # Not affected

    def test_invalid_environment_raises(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_ENV", "staging")
        with pytest.raises((ValueError, RuntimeError)):
            Settings()


# ============================================================
# B1: WINNOW_ENV=test enables all mocks
# ============================================================


class TestEnvironmentTestMode:
    """B1: WINNOW_ENV=test enables all mocks via computed properties."""

    def test_winnow_env_test_enables_all_mocks(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_ENV", "test")
        s = Settings()
        assert s.use_mock_llm is True
        assert s.use_mock_memos is True

    def test_winnow_env_test_raw_fields_still_false(self, monkeypatch):
        """Raw fields remain False — computed properties handle the logic."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_ENV", "test")
        s = Settings()
        # Raw fields are False (not explicitly set)
        assert s.mock_llm is False
        assert s.mock_memos is False
        # But computed properties are True
        assert s.use_mock_llm is True
        assert s.use_mock_memos is True

    def test_environment_alias(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "test")
        s = Settings()
        assert s.environment == "test"
        assert s.use_mock_llm is True

    def test_winnow_env_production_no_mocks(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_ENV", "production")
        s = Settings()
        assert s.use_mock_llm is False
        assert s.use_mock_memos is False


# ============================================================
# B2: Deprecated env vars emit DeprecationWarning
# ============================================================


class TestDeprecatedEnvVars:
    """B2: Old env vars still work but emit DeprecationWarning."""

    def test_real_llm_false_deprecation_warning(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_REAL_LLM", "false")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            s = Settings()
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1
            assert "WINNOW_REAL_LLM" in str(deprecation_warnings[0].message)
        assert s.use_mock_llm is True

    def test_real_llm_true_no_warning(self, monkeypatch):
        """WINNOW_REAL_LLM=true does NOT trigger deprecation (still real mode)."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_REAL_LLM", "true")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            s = Settings()
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            # Should NOT warn when value is "true" (not deprecated usage)
            llm_warnings = [x for x in deprecation_warnings if "REAL_LLM" in str(x.message)]
            assert len(llm_warnings) == 0
        assert s.use_mock_llm is False

    def test_real_llm_0_variant(self, monkeypatch):
        """WINNOW_REAL_LLM=0 triggers deprecation."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_REAL_LLM", "0")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            s = Settings()
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1
        assert s.use_mock_llm is True


# ============================================================
# R1: Consumer integration via get_settings()
# ============================================================


class TestConsumerIntegration:
    """R1: Settings fields control real mock/real switching in consumers."""

    def test_data_client_factory_production(self, monkeypatch):
        """Production mode (data_source=ciphertalk) → create_data_client returns CipherTalkClient."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_DATA_SOURCE", "ciphertalk")
        from z_winnow.config.settings import reset_settings

        reset_settings()  # 清单例, 让 get_settings 重读 env
        from z_winnow.pipeline.cipher_talk_client import create_data_client

        try:
            client = create_data_client()
            assert type(client).__name__ == "CipherTalkClient"
        finally:
            reset_settings()  # 清理, 避免影响后续测试

    def test_data_client_factory_weflow(self, monkeypatch):
        """WINNOW_DATA_SOURCE=weflow → create_data_client returns WeFlowClient."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_DATA_SOURCE", "weflow")
        from z_winnow.config.settings import reset_settings

        reset_settings()  # 清单例, 让 get_settings 重读 env
        from z_winnow.pipeline.cipher_talk_client import create_data_client

        try:
            client = create_data_client()
            assert type(client).__name__ == "WeFlowClient"
        finally:
            reset_settings()  # 清理, 避免影响后续测试

    def test_memos_factory_production(self, monkeypatch):
        """Production mode → mock_memos is False."""
        _clean_mock_env(monkeypatch)
        s = Settings()
        assert s.use_mock_memos is False

    def test_memos_factory_test_env(self, monkeypatch):
        """WINNOW_ENV=test → create_memos_adapter returns MockMemOSAdapter."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_ENV", "test")
        from z_winnow.memory.factory import create_memos_adapter
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = create_memos_adapter()
        assert isinstance(adapter, MockMemOSAdapter)

    def test_memos_factory_mock_memos(self, monkeypatch):
        """WINNOW_MOCK_MEMOS=true → MockMemOSAdapter."""
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_MOCK_MEMOS", "true")
        from z_winnow.memory.factory import create_memos_adapter
        from z_winnow.memory.mock_adapter import MockMemOSAdapter

        adapter = create_memos_adapter()
        assert isinstance(adapter, MockMemOSAdapter)


# ============================================================
# Anti-pattern audit: no os.getenv for mock variables
# ============================================================


class TestAntiPatternAudit:
    """B1: Verify no direct os.getenv for mock variables in consumer modules."""

    def test_no_os_getenv_in_memory_factory(self):
        """memory/factory.py has no executable os.getenv for deprecated vars."""
        import io
        import tokenize

        from z_winnow.memory import factory

        source = inspect.getsource(factory)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        for i, tok in enumerate(tokens):
            if (
                tok.type == tokenize.NAME
                and tok.string == "os"
                and i + 2 < len(tokens)
                and tokens[i + 1].string == "."
                and tokens[i + 2].type == tokenize.NAME
                and tokens[i + 2].string == "getenv"
            ):
                for j in range(i + 3, min(i + 10, len(tokens))):
                    if tokens[j].type == tokenize.STRING:
                        var_name = tokens[j].string.strip("'\"")
                        if var_name == "WEFLOW_MOCK_MODE" or var_name == "WINNOW_REAL_WEFLOW":
                            raise AssertionError(
                                f"factory.py: os.getenv('{var_name}') found — should use Settings"
                            )


# ============================================================
# get_settings() singleton integration
# ============================================================


class TestGetSettingsIntegration:
    """Verify get_settings() correctly loads mock control fields."""

    def test_get_settings_production_mode(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        s = get_settings()
        assert s.environment == "production"
        assert s.use_mock_llm is False

    def test_get_settings_test_mode(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        monkeypatch.setenv("WINNOW_ENV", "test")
        s = get_settings()
        assert s.environment == "test"
        assert s.use_mock_llm is True
        assert s.use_mock_memos is True

    def test_reset_picks_up_env_change(self, monkeypatch):
        _clean_mock_env(monkeypatch)
        s1 = get_settings()
        assert s1.use_mock_llm is False

        monkeypatch.setenv("WINNOW_MOCK_LLM", "true")
        reset_settings()
        s2 = get_settings()
        assert s2.use_mock_llm is True
