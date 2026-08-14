"""z-winnow test suite.

W12 纠偏后重建。每张纠偏卡片的 builder 负责创建对应测试文件。

本 conftest 注册 pytest 标记，并提供**一个**共享 autouse fixture
``_neutralize_config_overrides``：中和 onboarding 向导写的
``data/config_overrides.json``（详见 fixture 文档）。除此之外不覆盖任何 mock
模式 —— 让真实环境生效。各测试文件仍自行构造数据（内联 dict / 文件内
``_make_*``/``_seed_*`` 辅助），无集中数据工厂。
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: slow tests (>10s)")
    config.addinivalue_line("markers", "integration: integration tests needing external services")
    config.addinivalue_line("markers", "e2e: end-to-end tests needing full infrastructure stack")


@pytest.fixture(autouse=True)
def _neutralize_config_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite-wide: neutralize ``data/config_overrides.json`` for every test.

    ``get_settings()`` 把向导写入的该文件作为 **init kwargs** 注入
    (``Settings(**_load_overrides())``)，而 init kwargs 优先级高于环境变量 / .env。
    所以其中持久化的值（尤其 ``db_path=data/winnow.db``）会无视测试设的
    ``WINNOW_DB_PATH``，把测试钉死在真实 DB 上 —— 跨 run 泄漏 seed 行
    (UNIQUE 冲突)、并破坏「环境变量驱动设置」类断言。

    令 ``_load_overrides`` 返回 ``{}``，让所有测试只走 env / .env / default，
    环境变量驱动的隔离（如 ``WINNOW_DB_PATH`` → ``tmp_path``）才真正生效。
    autouse ⇒ 全套件生效一次，单一归属；与既有在文件内做同样中和的
    test_web_services / test_settings_convergence 完全兼容（重复无害）。
    """
    monkeypatch.setattr("z_winnow.config.settings._load_overrides", lambda: {})


@pytest.fixture(autouse=True)
def _cleanup_mockmock_files():
    """Remove stray ``<MagicMock ...>`` files from the project root after each test.

    Some tests ``patch(get_settings)`` without setting ``.db_path``; when the code
    under test then does ``Path(get_settings().db_path)`` / ``open(...)``, Python
    coerces the mock to its ``repr()`` and creates a 0-byte file named after it in
    the CWD. The real fix is to set ``db_path`` on every such mock (see e.g.
    ``test_memos_required_service``); this fixture is a safety net so any remaining
    or future culprit cannot litter the working tree.
    """
    yield
    import contextlib
    import pathlib

    for p in pathlib.Path.cwd().glob("<*MagicMock*>"):
        with contextlib.suppress(OSError):
            p.unlink()
