"""#9.2 日报配图工具链机制测试。

机制守卫（用户铁律：不作功能验证依据）——只守代码机制：
  - build_image_prompt 拼装含关键串（防模板漏拷）
  - call_dmx_api native Gemini 形态（x-goog-api-key 鉴权 + generateContent 端点 + 响应解析）
  - distill_daily_content 调一次 LLM 并返回内容 / 失败抛 ImageGenError
  - generate_cover dry_run 写 prompt.txt 不调 DMX；非 dry_run 落 cover.png

不证明真 API 能用；真 API 端到端见 handoff 验证记录。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

DATE = "20260709"
GROUP = "g_testimg"

DAILY: dict[str, Any] = {
    "date": DATE,
    "overview": "今日讨论了 A 和 B 两个话题，得出关键结论。",
    "topics": [
        {
            "topic_name": "话题A",
            "lifecycle": "sustained",
            "status": "active",
            "conclusion": "采用方案X",
            "participants": ["张三", "李四"],
            "weight": 0.8,
        },
        {
            "topic_name": "话题B",
            "lifecycle": "emerging",
            "status": "discussion",
            "conclusion": "待验证",
            "participants": ["王五"],
            "weight": 0.4,
        },
    ],
    "highlights": ["「方案X 比方案Y 快 40%」"],
    "trend_summary": "今日2个议题，1持续1新增。",
}


def _reset_settings() -> None:
    from z_winnow.config.settings import reset_settings

    reset_settings()


class _FakeMsg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeModel:
    """假 LLM：ainvoke 返回固定内容。"""

    def __init__(self, content: str = "【主题】测试主题\n【核心议题与观点】1. 话题A：采用方案X") -> None:
        self._content = content
        self.calls = 0

    async def ainvoke(self, prompt: str, **kw: Any) -> _FakeMsg:
        self.calls += 1
        return _FakeMsg(self._content)


# ============================================================
# build_image_prompt
# ============================================================


def test_build_image_prompt_assembles_three_parts() -> None:
    """prompt 含提炼内容 + 生图模板基础风格 + 日报风格(人物名)。"""
    from z_winnow.outputs.image_gen import build_image_prompt

    prompt = build_image_prompt("MY_DISTILLED_CONTENT_XYZ")
    assert "MY_DISTILLED_CONTENT_XYZ" in prompt  # 提炼内容被包装进生图模板
    assert "信息图表风格" in prompt or "信息图" in prompt  # 生图模板/风格基础
    assert "人物名" in prompt  # 日报生图风格.md 独有（不许遗漏）
    assert "内容如下" in prompt  # 生图模板.txt 开头标记


# ============================================================
# call_dmx_api (native Gemini)
# ============================================================


async def test_call_dmx_api_native_gemini_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """x-goog-api-key 鉴权（非 Bearer）+ /v1beta/models/{model}:generateContent + 响应解析。"""
    monkeypatch.setenv("WINNOW_QUICK_IMG_API_KEY", "sk-test-123")
    _reset_settings()

    captured: dict[str, str] = {}
    img = b"\x89PNG\r\n\x1a\n fake png bytes"

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["x_goog_api_key"] = req.headers.get("x-goog-api-key", "")
        captured["authorization"] = req.headers.get("authorization", "")
        body = {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"data": base64.b64encode(img).decode()}}]}}
            ]
        }
        return httpx.Response(200, json=body)

    from z_winnow.outputs.image_gen import call_dmx_api

    out = await call_dmx_api("p", ratio="4:5", size="2K", _transport=httpx.MockTransport(handler))
    assert out == img
    assert captured["x_goog_api_key"] == "sk-test-123"
    assert captured["authorization"] == ""  # 不带 Bearer
    assert "/v1beta/models/" in captured["url"]
    assert ":generateContent" in captured["url"]


async def test_call_dmx_api_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """无 API key → ImageGenConfigError（三个 alias 全清空）。"""
    for k in ("WINNOW_QUICK_IMG_API_KEY", "QUICK_IMG_API_KEY", "DMX_API_KEY"):
        monkeypatch.setenv(k, "")
    _reset_settings()

    from z_winnow.outputs.image_gen import ImageGenConfigError, call_dmx_api

    with pytest.raises(ImageGenConfigError):
        await call_dmx_api("p")


async def test_call_dmx_api_non_200_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINNOW_QUICK_IMG_API_KEY", "sk-test")
    _reset_settings()

    from z_winnow.outputs.image_gen import ImageGenError, call_dmx_api

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream boom")

    with pytest.raises(ImageGenError, match="500"):
        await call_dmx_api("p", _transport=httpx.MockTransport(handler))


async def test_call_dmx_api_no_image_data_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINNOW_QUICK_IMG_API_KEY", "sk-test")
    _reset_settings()

    from z_winnow.outputs.image_gen import ImageGenError, call_dmx_api

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "no image"}]}}]})

    with pytest.raises(ImageGenError):
        await call_dmx_api("p", _transport=httpx.MockTransport(handler))


# ============================================================
# distill_daily_content (纯 LLM，monkeypatch model factory)
# ============================================================


async def test_distill_invokes_llm_and_returns_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from z_winnow.config import models

    fake = _FakeModel("【主题】单测\n【核心议题与观点】1. 话题A：采用方案X")
    monkeypatch.setattr(models, "create_model_for_subagent", lambda *a, **k: fake)

    from z_winnow.outputs.image_gen import distill_daily_content

    out = await distill_daily_content(DAILY)
    assert fake.calls == 1
    assert "【主题】单测" in out


async def test_distill_llm_call_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadModel:
        async def ainvoke(self, prompt: str, **kw: Any) -> Any:
            raise RuntimeError("api down")

    from z_winnow.config import models

    monkeypatch.setattr(models, "create_model_for_subagent", lambda *a, **k: _BadModel())

    from z_winnow.outputs.image_gen import ImageGenError, distill_daily_content

    with pytest.raises(ImageGenError, match="提炼 LLM 调用失败"):
        await distill_daily_content(DAILY)


def test_daily_to_text_orders_by_weight_and_aggregates_members() -> None:
    """机制：_daily_to_text 按权重降序、聚合去重成员（喂 LLM 的输入质量守卫）。"""
    from z_winnow.outputs.image_gen import _daily_to_text

    text = _daily_to_text(DAILY)
    # 高权重话题A(0.8) 应排在低权重话题B(0.4) 之前
    assert text.index("话题A") < text.index("话题B")
    assert "张三" in text and "李四" in text and "王五" in text
    assert "采用方案X" in text  # conclusion


# ============================================================
# generate_cover (编排)
# ============================================================


@pytest.fixture
def l3_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "processed" / GROUP / DATE
    d.mkdir(parents=True)
    (d / "daily.json").write_text(json.dumps(DAILY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", str(tmp_path / "processed"))
    _reset_settings()
    return d


async def test_generate_cover_dry_run_writes_prompt_no_dmx(
    l3_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from z_winnow.outputs import image_gen

    async def fake_distill(daily_data: dict[str, Any]) -> str:
        return "【主题】dry run 提炼结果"

    dmx_called = 0

    async def fake_dmx(*a: Any, **kw: Any) -> bytes:  # should NOT be called
        nonlocal dmx_called
        dmx_called += 1
        return b""

    monkeypatch.setattr(image_gen, "distill_daily_content", fake_distill)
    monkeypatch.setattr(image_gen, "call_dmx_api", fake_dmx)

    paths = await image_gen.generate_cover(GROUP, DATE, dry_run=True)
    assert dmx_called == 0
    prompt_txt = l3_dir / "cover.prompt.txt"
    assert prompt_txt.is_file()
    assert "dry run 提炼结果" in prompt_txt.read_text(encoding="utf-8")
    assert not (l3_dir / "cover.png").exists()
    assert paths == [prompt_txt]


async def test_generate_cover_real_writes_png(
    l3_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from z_winnow.outputs import image_gen

    async def fake_distill(daily_data: dict[str, Any]) -> str:
        return "【主题】real"

    fake_bytes = b"\x89PNG\r\n\x1a\n cover image"

    async def fake_dmx(_prompt: str, **_kw: Any) -> bytes:
        return fake_bytes

    monkeypatch.setattr(image_gen, "distill_daily_content", fake_distill)
    monkeypatch.setattr(image_gen, "call_dmx_api", fake_dmx)

    paths = await image_gen.generate_cover(GROUP, DATE)  # dry_run=False
    cover = l3_dir / "cover.png"
    assert cover.is_file()
    assert cover.read_bytes() == fake_bytes
    assert paths == [cover]


async def test_generate_cover_missing_l3_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINNOW_LAYER3_OUTPUT_DIR", str(tmp_path / "empty"))
    _reset_settings()
    from z_winnow.outputs.image_gen import generate_cover

    with pytest.raises(FileNotFoundError, match=r"daily\.json"):
        await generate_cover(GROUP, DATE, dry_run=True)
