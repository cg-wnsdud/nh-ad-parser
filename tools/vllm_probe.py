#!/usr/bin/env python3
"""농협 내부 vLLM의 멀티모달 + strict JSON Schema 호환성 점검.

표준 라이브러리만 사용하며 주소, 모델명, 선택적 API key는 환경변수/.env에서 읽는다.
실제 광고나 고객 문서는 전송하지 않고 내장된 작은 흰색 PNG만 사용한다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# 16x16 흰색 RGB PNG. 고객 데이터가 아닌 고정 테스트 자산이다.
_WHITE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFElEQVR4nGP8"
    "//8/AymAiSTVoxpHAAANHQEfNqXJAAAAAElFTkSuQmCC"
)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _payload(model: str) -> dict:
    image = base64.b64encode(_WHITE_PNG).decode("ascii")
    return {
        "model": model,
        "temperature": 0,
        "max_tokens": 100,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "첨부 이미지를 확인하고 status는 반드시 ok, description은 "
                        "짧은 문자열로 응답하세요."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image}"},
                },
            ],
        }],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "nh_vllm_probe",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["ok"]},
                        "description": {"type": "string"},
                    },
                    "required": ["status", "description"],
                    "additionalProperties": False,
                },
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="농협 내부 vLLM 호환성 smoke test")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--url")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    _load_env_file(args.env_file)

    url = (args.url or os.getenv("GEMMA_URL", "")).strip()
    model = (args.model or os.getenv("GEMMA_MODEL", "")).strip()
    if not url or not model:
        print("FAIL: GEMMA_URL과 GEMMA_MODEL이 필요합니다.")
        return 2

    body = json.dumps(_payload(model), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    api_key = os.getenv("GEMMA_API_KEY", "").strip()
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        print(f"FAIL: HTTP {exc.code}\n{raw[:2000]}")
        return 1
    except urllib.error.URLError as exc:
        print(f"FAIL: 연결 오류 - {exc.reason}")
        return 1

    try:
        envelope = json.loads(raw)
        content = envelope["choices"][0]["message"]["content"]
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        print(f"FAIL: OpenAI/JSON Schema 응답 형식이 아님 - {exc}\n{raw[:2000]}")
        return 1
    if status != 200 or result.get("status") != "ok":
        print(f"FAIL: 예상 응답이 아님 - HTTP {status}, {result}")
        return 1
    print(f"OK: HTTP {status}, model={model}, multimodal=true, strict_json_schema=true")
    print(f"description={result.get('description', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

