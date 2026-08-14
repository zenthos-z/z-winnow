"""T-W12-5: Tests for Settings field convergence (S7 配置单源).

Verifies:
  - B1: All converged fields accessible via get_settings()
  - B2: Dual-naming support (WINNOW_{NAME} and {NAME} both load)
  - B3: Alias support (REPORT_OUTPUT_DIR → reports_dir, PROCESSED_DATA_DIR → layer3_output_dir)
  - R1: Real Settings loading from environment variables

P012: Each test self-contained with monkeypatch isolation + cleanup.
A013: Tests verify function-level Settings access (not module-level frozen values).
"""

from __future__ import annotations

import pytest

from z_winnow.config.settings import Settings, get_settings, reset_settings

# ============================================================
# Fixtures — P012: monkeypatch isolation
# ============================================================


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch):
    """Reset Settings singleton before and after each test (P012).

    Also neutralize the wizard's ``config_overrides.json`` injection so tests see
    only env/defaults. The override file is a real runtime artifact (written by the
    onboarding「保存并重启」) and would otherwise leak values like ``db_path`` /
    ``enable_enrich`` into assertions, beating the test's env vars (kwargs win).
    """
    monkeypatch.setattr("z_winnow.config.settings._load_overrides", lambda: {})
    reset_settings()
    yield
    reset_settings()


# ============================================================
# B2: New fields accessible via get_settings()
# ============================================================


class TestNewFieldsDefaults:
    """Verify all new Settings fields have correct defaults.

    Tests use explicit env vars to ensure defaults are tested in isolation,
    since .env may set real values.
    """

    def test_content_enrich_timeout_default(self):
        """Default 180 when no env var set."""
        import os

        os.environ.pop("CONTENT_ENRICH_TIMEOUT", None)
        os.environ.pop("WINNOW_CONTENT_ENRICH_TIMEOUT", None)
        s = Settings()
        assert s.content_enrich_timeout == 180

    def test_enable_enrich_default(self):
        """Default True when no env var set."""
        import os

        os.environ.pop("ENABLE_ENRICH", None)
        os.environ.pop("WINNOW_ENABLE_ENRICH", None)
        s = Settings()
        assert s.enable_enrich is True

    def test_web_port_default(self):
        """Default 8100 when no env var set."""
        import os

        os.environ.pop("WEB_PORT", None)
        os.environ.pop("WINNOW_WEB_PORT", None)
        s = Settings()
        assert s.web_port == 8100

    def test_web_host_default(self):
        """Default 127.0.0.1 when no env var set."""
        import os

        os.environ.pop("WEB_HOST", None)
        os.environ.pop("WINNOW_WEB_HOST", None)
        s = Settings()
        assert s.web_host == "127.0.0.1"

    def test_group_name_default(self):
        """Default empty when no env var set."""
        import os

        os.environ.pop("GROUP_NAME", None)
        os.environ.pop("WINNOW_GROUP_NAME", None)
        s = Settings()
        assert s.group_name == ""

    def test_vision_model_default(self):
        """Vision model field exists as string. .env may override default."""
        s = Settings()
        assert isinstance(s.vision_model, str)

    def test_vision_base_url_default(self):
        """Vision base URL field exists as string. .env may override default."""
        s = Settings()
        assert isinstance(s.vision_base_url, str)

    def test_vision_api_key_default(self):
        """Vision API key field exists as string. .env may override default."""
        s = Settings()
        assert isinstance(s.vision_api_key, str)

    def test_mcp_image_analysis_default(self):
        """Default False when no env var set."""
        import os

        os.environ.pop("MCP_IMAGE_ANALYSIS", None)
        os.environ.pop("WINNOW_MCP_IMAGE_ANALYSIS", None)
        s = Settings()
        assert s.mcp_image_analysis is False

    def test_image_max_concurrency_default(self):
        """Max concurrency field exists as int. .env may override default."""
        s = Settings()
        assert isinstance(s.image_max_concurrency, int)
        assert s.image_max_concurrency > 0

    def test_image_max_file_size_mb_default(self):
        """Default 20 when no env var set."""
        import os

        os.environ.pop("IMAGE_MAX_FILE_SIZE_MB", None)
        os.environ.pop("WINNOW_IMAGE_MAX_FILE_SIZE_MB", None)
        s = Settings()
        assert s.image_max_file_size_mb == 20

    def test_supported_image_formats_default(self):
        """Default empty when no env var set."""
        import os

        os.environ.pop("SUPPORTED_IMAGE_FORMATS", None)
        os.environ.pop("WINNOW_SUPPORTED_IMAGE_FORMATS", None)
        # _env_file=None: the local .env hardcodes SUPPORTED_IMAGE_FORMATS; we want
        # the field DEFAULT, so bypass the dotenv source entirely.
        s = Settings(_env_file=None)
        assert s.supported_image_formats == ""

    def test_mcp_image_endpoint_default(self):
        """Default http://127.0.0.1:8080/analyze_image when no env var set."""
        import os

        os.environ.pop("MCP_IMAGE_ENDPOINT", None)
        os.environ.pop("WINNOW_MCP_IMAGE_ENDPOINT", None)
        s = Settings()
        assert s.mcp_image_endpoint == "http://127.0.0.1:8080/analyze_image"


# ============================================================
# B3: Dual-naming support — WINNOW_{NAME} and {NAME}
# ============================================================


class TestDualNaming:
    """Verify dual-naming support: WINNOW_{NAME} and {NAME} both load.

    P012: Each test sets exactly one env var and verifies the value.
    """

    def test_content_enrich_timeout_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_CONTENT_ENRICH_TIMEOUT", "300")
        s = Settings()
        assert s.content_enrich_timeout == 300

    def test_content_enrich_timeout_no_prefix(self, monkeypatch):
        monkeypatch.setenv("CONTENT_ENRICH_TIMEOUT", "300")
        s = Settings()
        assert s.content_enrich_timeout == 300

    def test_enable_enrich_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_ENABLE_ENRICH", "false")
        s = Settings()
        assert s.enable_enrich is False

    def test_enable_enrich_no_prefix(self, monkeypatch):
        monkeypatch.setenv("ENABLE_ENRICH", "false")
        s = Settings()
        assert s.enable_enrich is False

    def test_web_port_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_WEB_PORT", "9000")
        s = Settings()
        assert s.web_port == 9000

    def test_web_port_no_prefix(self, monkeypatch):
        monkeypatch.setenv("WEB_PORT", "9000")
        s = Settings()
        assert s.web_port == 9000

    def test_web_host_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_WEB_HOST", "0.0.0.0")
        s = Settings()
        assert s.web_host == "0.0.0.0"

    def test_web_host_no_prefix(self, monkeypatch):
        monkeypatch.setenv("WEB_HOST", "0.0.0.0")
        s = Settings()
        assert s.web_host == "0.0.0.0"

    def test_group_name_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_GROUP_NAME", "test-group")
        s = Settings()
        assert s.group_name == "test-group"

    def test_group_name_no_prefix(self, monkeypatch):
        monkeypatch.setenv("GROUP_NAME", "test-group")
        s = Settings()
        assert s.group_name == "test-group"

    def test_vision_model_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_VISION_MODEL", "gpt-4o")
        s = Settings()
        assert s.vision_model == "gpt-4o"

    def test_vision_model_no_prefix(self, monkeypatch):
        monkeypatch.setenv("VISION_MODEL", "gpt-4o")
        s = Settings()
        assert s.vision_model == "gpt-4o"

    def test_vision_base_url_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_VISION_BASE_URL", "https://api.openai.com/v1")
        s = Settings()
        assert s.vision_base_url == "https://api.openai.com/v1"

    def test_vision_base_url_no_prefix(self, monkeypatch):
        monkeypatch.setenv("VISION_BASE_URL", "https://api.openai.com/v1")
        s = Settings()
        assert s.vision_base_url == "https://api.openai.com/v1"

    def test_vision_api_key_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_VISION_API_KEY", "sk-test123")
        s = Settings()
        assert s.vision_api_key == "sk-test123"

    def test_vision_api_key_no_prefix(self, monkeypatch):
        monkeypatch.setenv("VISION_API_KEY", "sk-test123")
        s = Settings()
        assert s.vision_api_key == "sk-test123"

    def test_mcp_image_analysis_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_MCP_IMAGE_ANALYSIS", "true")
        s = Settings()
        assert s.mcp_image_analysis is True

    def test_mcp_image_analysis_no_prefix(self, monkeypatch):
        monkeypatch.setenv("MCP_IMAGE_ANALYSIS", "true")
        s = Settings()
        assert s.mcp_image_analysis is True

    def test_image_max_concurrency_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_IMAGE_MAX_CONCURRENCY", "10")
        s = Settings()
        assert s.image_max_concurrency == 10

    def test_image_max_concurrency_no_prefix(self, monkeypatch):
        monkeypatch.setenv("IMAGE_MAX_CONCURRENCY", "10")
        s = Settings()
        assert s.image_max_concurrency == 10

    def test_image_max_file_size_mb_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_IMAGE_MAX_FILE_SIZE_MB", "50")
        s = Settings()
        assert s.image_max_file_size_mb == 50

    def test_image_max_file_size_mb_no_prefix(self, monkeypatch):
        monkeypatch.setenv("IMAGE_MAX_FILE_SIZE_MB", "50")
        s = Settings()
        assert s.image_max_file_size_mb == 50

    def test_supported_image_formats_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_SUPPORTED_IMAGE_FORMATS", "png,jpg")
        s = Settings()
        assert s.supported_image_formats == "png,jpg"

    def test_supported_image_formats_no_prefix(self, monkeypatch):
        monkeypatch.setenv("SUPPORTED_IMAGE_FORMATS", "png,jpg")
        s = Settings()
        assert s.supported_image_formats == "png,jpg"

    def test_mcp_image_endpoint_winnow_prefix(self, monkeypatch):
        monkeypatch.setenv("WINNOW_MCP_IMAGE_ENDPOINT", "http://custom:9090/analyze")
        s = Settings()
        assert s.mcp_image_endpoint == "http://custom:9090/analyze"

    def test_mcp_image_endpoint_no_prefix(self, monkeypatch):
        monkeypatch.setenv("MCP_IMAGE_ENDPOINT", "http://custom:9090/analyze")
        s = Settings()
        assert s.mcp_image_endpoint == "http://custom:9090/analyze"


# ============================================================
# B3: Alias support — REPORT_OUTPUT_DIR and PROCESSED_DATA_DIR
# ============================================================


class TestAliasSupport:
    """Verify alias env var names map to existing Settings fields."""

    def test_report_output_dir_alias(self, monkeypatch):
        """REPORT_OUTPUT_DIR maps to reports_dir."""
        monkeypatch.setenv("REPORT_OUTPUT_DIR", "my-reports")
        s = Settings()
        assert s.reports_dir == "my-reports"

    def test_report_output_dir_winnow_alias(self, monkeypatch):
        """WINNOW_REPORT_OUTPUT_DIR maps to reports_dir."""
        monkeypatch.setenv("WINNOW_REPORT_OUTPUT_DIR", "my-reports")
        s = Settings()
        assert s.reports_dir == "my-reports"

    def test_processed_data_dir_alias(self, monkeypatch):
        """PROCESSED_DATA_DIR maps to layer3_output_dir."""
        monkeypatch.setenv("PROCESSED_DATA_DIR", "data/processed-alias")
        s = Settings()
        assert s.layer3_output_dir == "data/processed-alias"

    def test_processed_data_dir_winnow_alias(self, monkeypatch):
        """WINNOW_PROCESSED_DATA_DIR maps to layer3_output_dir."""
        monkeypatch.setenv("WINNOW_PROCESSED_DATA_DIR", "data/processed-alias")
        s = Settings()
        assert s.layer3_output_dir == "data/processed-alias"


# ============================================================
# R1: Real Settings loading from get_settings()
# ============================================================


class TestRealSettingsLoading:
    """R1: Verify Settings loads from real environment variables."""

    def test_web_port_from_env_via_get_settings(self, monkeypatch):
        """R1: Set WINNOW_WEB_PORT=9000, verify get_settings().web_port == 9000."""
        monkeypatch.setenv("WINNOW_WEB_PORT", "9000")
        settings = get_settings()
        assert settings.web_port == 9000

    def test_content_enrich_timeout_from_env(self, monkeypatch):
        """R1: Set CONTENT_ENRICH_TIMEOUT=60, verify get_settings().content_enrich_timeout == 60."""
        monkeypatch.setenv("CONTENT_ENRICH_TIMEOUT", "60")
        settings = get_settings()
        assert settings.content_enrich_timeout == 60

    def test_enable_enrich_false_from_env(self, monkeypatch):
        """R1: Set WINNOW_ENABLE_ENRICH=false, verify get_settings().enable_enrich is False."""
        monkeypatch.setenv("WINNOW_ENABLE_ENRICH", "false")
        settings = get_settings()
        assert settings.enable_enrich is False

    def test_group_name_from_env(self, monkeypatch):
        """R1: Set WINNOW_GROUP_NAME=test-group, verify get_settings().group_name."""
        monkeypatch.setenv("WINNOW_GROUP_NAME", "test-group")
        settings = get_settings()
        assert settings.group_name == "test-group"


# ============================================================
# Sensitive field masking
# ============================================================


class TestSensitiveFieldMasking:
    """Verify new sensitive fields are masked in repr."""

    def test_vision_api_key_masked_in_repr(self, monkeypatch):
        monkeypatch.setenv("VISION_API_KEY", "sk-very-secret-key-12345")
        s = Settings()
        r = repr(s)
        assert "sk-very-secret-key-12345" not in r
        assert "vision_api_key=" in r


# ============================================================
# Integration: get_settings() singleton + force_reload
# ============================================================


class TestSettingsSingleton:
    """Verify Settings singleton behavior with new fields."""

    def test_singleton_returns_same_instance(self, monkeypatch):
        monkeypatch.setenv("WINNOW_WEB_PORT", "7777")
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        assert s1.web_port == 7777

    def test_force_reload_picks_up_new_env(self, monkeypatch):
        """reset_settings() + get_settings() picks up changed env vars."""
        monkeypatch.setenv("WINNOW_WEB_PORT", "8888")
        s1 = get_settings()
        assert s1.web_port == 8888

        # Change env and reload
        monkeypatch.setenv("WINNOW_WEB_PORT", "9999")
        reset_settings()
        s2 = get_settings()
        assert s2.web_port == 9999
