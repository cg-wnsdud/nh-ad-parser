from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vllm_probe_uses_image_and_same_strict_schema_contract():
    probe = _load("vllm_probe_for_test", "tools/vllm_probe.py")
    assert probe._WHITE_PNG.startswith(b"\x89PNG\r\n\x1a\n")
    payload = probe._payload("vision-model")
    assert payload["model"] == "vision-model"
    content = payload["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True


def test_runtime_check_env_file_does_not_override_exported_value(tmp_path, monkeypatch):
    checker = _load("nh_runtime_check_for_test", "tools/nh_runtime_check.py")
    env_file = tmp_path / ".env"
    env_file.write_text("ETL_WS_ID=file-value\nETL_AUTHOR=file-author\n", encoding="utf-8")
    monkeypatch.setenv("ETL_WS_ID", "exported-value")
    monkeypatch.delenv("ETL_AUTHOR", raising=False)

    checker._load_env_file(env_file)

    assert checker.os.environ["ETL_WS_ID"] == "exported-value"
    assert checker.os.environ["ETL_AUTHOR"] == "file-author"
