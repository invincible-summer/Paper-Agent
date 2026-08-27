"""Native multimodal unified-channel tests (offline; no provider calls).

Covers config parsing and activation rules for NATIVE_MULTIMODAL_*, provider
routing in get_llm / get_vision_client, the ainvoke_utility thinking-disable
skip, and the analyze() default max_tokens fallback.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import core.config as cfg_mod
import core.llm as llm_mod

try:
    from core.config import (
        LLMConfig,
        MultimodalConfig,
        NativeMultimodalConfig,
        Settings,
        load_settings,
        native_multimodal_active,
    )
except ImportError:  # NATIVE_MULTIMODAL_* 尚未实现；落地后本模块自动恢复收集
    pytest.skip(
        "NATIVE_MULTIMODAL_* is not implemented yet "
        "(core.config lacks NativeMultimodalConfig); skipping WIP tests",
        allow_module_level=True,
    )

from core.multimodal import vision_client as vc
from core.multimodal.vision_client import VisionClient, get_vision_client


class _FakeCache:
    def get(self, *a, **k):
        return None

    def put(self, *a, **k):
        return None


def _native_settings(**kw) -> Settings:
    s = Settings()
    s.native_multimodal = NativeMultimodalConfig(**kw)
    return s


def _set_env(monkeypatch, **pairs):
    for key, val in pairs.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)


# --- config parsing / activation rules -----------------------------------------


def test_native_config_env_parsing(monkeypatch):
    _set_env(
        monkeypatch,
        NATIVE_MULTIMODAL_API_KEY="sk-native",
        NATIVE_MULTIMODAL_BASE_URL="https://open.bigmodel.cn/api/paas/v4",
        NATIVE_MULTIMODAL_MODEL="glm-5.3-flash",
        NATIVE_MULTIMODAL_TEMPERATURE="0.7",
        NATIVE_MULTIMODAL_TOP_P="0.9",
        NATIVE_MULTIMODAL_EXTRA_BODY='{"reasoning_effort":"max"}',
    )
    s = load_settings()
    n = s.native_multimodal
    assert (n.api_key, n.model) == ("sk-native", "glm-5.3-flash")
    assert n.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert n.temperature == 0.7 and n.top_p == 0.9
    assert n.extra_body == {"reasoning_effort": "max"}
    assert native_multimodal_active(s) is True


def test_native_extra_body_invalid_json_ignored(monkeypatch, caplog):
    _set_env(
        monkeypatch,
        NATIVE_MULTIMODAL_API_KEY="k",
        NATIVE_MULTIMODAL_BASE_URL="http://x/v1",
        NATIVE_MULTIMODAL_MODEL="m",
        NATIVE_MULTIMODAL_EXTRA_BODY="not-json{",
    )
    s = load_settings()
    assert s.native_multimodal.extra_body == {}
    with caplog.at_level(logging.WARNING, logger="core.config"):
        assert native_multimodal_active(s) is True
    assert "EXTRA_BODY" in caplog.text


def test_native_unset_channel_is_inactive(monkeypatch):
    _set_env(
        monkeypatch,
        NATIVE_MULTIMODAL_API_KEY=None,
        NATIVE_MULTIMODAL_BASE_URL=None,
        NATIVE_MULTIMODAL_MODEL=None,
    )
    s = load_settings()
    assert native_multimodal_active(s) is False


def test_native_partial_fill_warns_and_falls_back(monkeypatch, caplog):
    _set_env(
        monkeypatch,
        NATIVE_MULTIMODAL_API_KEY=None,
        NATIVE_MULTIMODAL_BASE_URL="http://x/v1",
        NATIVE_MULTIMODAL_MODEL=None,
    )
    s = load_settings()
    with caplog.at_level(logging.WARNING, logger="core.config"):
        assert native_multimodal_active(s) is False
    assert "partially" in caplog.text
    # The warning fires once per Settings instance, not on every consult.
    caplog.clear()
    assert native_multimodal_active(s) is False
    assert "partially" not in caplog.text


def test_native_enabled_false_forces_split_mode(monkeypatch):
    _set_env(
        monkeypatch,
        NATIVE_MULTIMODAL_API_KEY="k",
        NATIVE_MULTIMODAL_BASE_URL="http://x/v1",
        NATIVE_MULTIMODAL_MODEL="m",
        NATIVE_MULTIMODAL_ENABLED="false",
    )
    s = load_settings()
    assert s.native_multimodal.enabled is False
    assert native_multimodal_active(s) is False


def test_native_enabled_true_cannot_activate_partial_fill(monkeypatch):
    _set_env(
        monkeypatch,
        NATIVE_MULTIMODAL_API_KEY=None,
        NATIVE_MULTIMODAL_BASE_URL="http://x/v1",
        NATIVE_MULTIMODAL_MODEL="m",
        NATIVE_MULTIMODAL_ENABLED="true",
    )
    s = load_settings()
    assert native_multimodal_active(s) is False


# --- get_llm provider routing ---------------------------------------------------


def test_get_llm_routes_to_native_channel(monkeypatch):
    s = _native_settings(
        api_key="nk", base_url="http://native/v1", model="glm-5.3-flash",
        temperature=0.9, top_p=0.9,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: s)
    llm_mod.get_llm.cache_clear()
    try:
        for tier in ("light", "reasoning"):
            r = llm_mod.get_llm(tier)
            inner = r._llm
            assert inner.model_name == "glm-5.3-flash"
            assert str(inner.openai_api_base).startswith("http://native/v1")
            assert inner.temperature == 0.9 and inner.top_p == 0.9
    finally:
        llm_mod.get_llm.cache_clear()


def test_get_llm_native_forwards_extra_body(monkeypatch):
    s = _native_settings(
        api_key="nk", base_url="http://native/v1", model="glm-5.3-flash",
        extra_body={"tool_stream": True},
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: s)
    llm_mod.get_llm.cache_clear()
    try:
        inner = llm_mod.get_llm("light")._llm
        payload = inner._get_request_payload([{"role": "user", "content": "hi"}])
        assert payload.get("extra_body") == {"tool_stream": True}
    finally:
        llm_mod.get_llm.cache_clear()


def test_get_llm_split_mode_unchanged(monkeypatch):
    s = Settings()
    s.llm = LLMConfig(
        api_key="dk", base_url="http://ds/v1",
        model_light="ds-flash", model_reasoning="ds-pro", temperature=0.3,
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: s)
    llm_mod.get_llm.cache_clear()
    try:
        assert llm_mod.get_llm("light")._llm.model_name == "ds-flash"
        assert llm_mod.get_llm("reasoning")._llm.model_name == "ds-pro"
        inner = llm_mod.get_llm("light")._llm
        assert str(inner.openai_api_base).startswith("http://ds/v1")
        assert inner.temperature == 0.3
    finally:
        llm_mod.get_llm.cache_clear()


# --- ainvoke_utility thinking-disable policy ------------------------------------


class _RecordingLLM:
    def __init__(self, fail_first_with=None):
        self.calls: list[dict] = []
        self._fail = fail_first_with

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self._fail and len(self.calls) == 1:
            raise RuntimeError(self._fail)
        return "ok"


def test_ainvoke_utility_skips_thinking_disable_in_native_mode(monkeypatch):
    s = _native_settings(api_key="k", base_url="u", model="m")
    monkeypatch.setattr(llm_mod, "get_settings", lambda: s)
    fake = _RecordingLLM()
    out = asyncio.run(llm_mod.ainvoke_utility(fake, []))
    assert out == "ok"
    assert len(fake.calls) == 1
    assert "extra_body" not in fake.calls[0]


def test_ainvoke_utility_native_optin_merges_provider_extra(monkeypatch):
    s = _native_settings(
        api_key="k", base_url="u", model="m",
        utility_disable_thinking=True, extra_body={"tool_stream": True},
    )
    monkeypatch.setattr(llm_mod, "get_settings", lambda: s)
    fake = _RecordingLLM()
    asyncio.run(llm_mod.ainvoke_utility(fake, []))
    assert fake.calls[0]["extra_body"] == {
        "tool_stream": True, "thinking": {"type": "disabled"},
    }


def test_ainvoke_utility_split_mode_still_sends_thinking_disable(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: Settings())
    fake = _RecordingLLM()
    asyncio.run(llm_mod.ainvoke_utility(fake, []))
    assert fake.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_ainvoke_utility_400_fallback_still_works(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_settings", lambda: Settings())
    fake = _RecordingLLM(fail_first_with="400 unknown parameter thinking")
    out = asyncio.run(llm_mod.ainvoke_utility(fake, []))
    assert out == "ok"
    assert len(fake.calls) == 2
    assert "extra_body" not in fake.calls[1]


# --- get_vision_client provider routing ------------------------------------------


def _patch_vision(monkeypatch, settings_obj):
    vc._CLIENT = None
    monkeypatch.setattr(vc, "get_settings", lambda: settings_obj)
    monkeypatch.setattr(vc, "get_vision_cache", lambda *a, **k: _FakeCache())


def test_get_vision_client_routes_to_native_channel(monkeypatch):
    s = _native_settings(
        api_key="nk", base_url="http://native/v1", model="glm-5.3-flash",
        vision_temperature=0.4, vision_max_tokens=2048, max_concurrent=5,
    )
    s.multimodal = MultimodalConfig()  # split channel empty on purpose
    _patch_vision(monkeypatch, s)
    try:
        c = get_vision_client()
        assert isinstance(c, VisionClient)
        assert c._cfg.model == "glm-5.3-flash"
        assert c._cfg.base_url == "http://native/v1"
        assert c._cfg.temperature == 0.4
        assert c._default_max_tokens == 2048
        assert c._llm.model_name == "glm-5.3-flash"
    finally:
        vc._CLIENT = None


def test_get_vision_client_prefers_native_over_multimodal(monkeypatch):
    s = _native_settings(api_key="nk", base_url="http://native/v1", model="glm-5.3-flash")
    s.multimodal = MultimodalConfig(api_key="sk", base_url="http://split/v1", model="gpt-5.6-luna")
    _patch_vision(monkeypatch, s)
    try:
        c = get_vision_client()
        assert c._cfg.model == "glm-5.3-flash"
        assert c._cfg.base_url == "http://native/v1"
    finally:
        vc._CLIENT = None


def test_get_vision_client_split_mode_when_native_off(monkeypatch):
    s = Settings()
    s.multimodal = MultimodalConfig(api_key="sk", base_url="http://split/v1", model="gpt-5.6-luna")
    _patch_vision(monkeypatch, s)
    try:
        c = get_vision_client()
        assert c._cfg.model == "gpt-5.6-luna"
        assert c._default_max_tokens == 1024  # historical default preserved
    finally:
        vc._CLIENT = None


def test_get_vision_client_none_when_both_channels_off(monkeypatch):
    _patch_vision(monkeypatch, Settings())
    assert get_vision_client() is None


# --- analyze() default max_tokens fallback ----------------------------------------


def test_analyze_max_tokens_falls_back_to_config_default(tmp_path):
    c = VisionClient(
        MultimodalConfig(api_key="k", base_url="http://x/v1", model="m",
                         response_max_tokens=2048)
    )
    c._cache = _FakeCache()
    captured = {}

    async def fake_call(messages, max_tokens):
        captured[max_tokens] = True
        return "{}"

    c._call_with_retry = fake_call
    asyncio.run(c.analyze(b"img-default", "figure", "p"))
    assert 2048 in captured
    asyncio.run(c.analyze(b"img-explicit", "figure", "p", max_tokens=512))
    assert 512 in captured
