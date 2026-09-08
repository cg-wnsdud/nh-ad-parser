# -*- coding: utf-8 -*-
"""팀장 검토용 A안: ETLwithLLM 공개 API → 현재 P1/P3 파이프라인.

실제 폐쇄망 반입 도구는 ``etl_probe.py``다. 이 도구는 연결 방식 확정 전 코드 리뷰와
목 서버 시험을 위한 프로토타입이며 프로젝트 의존성을 사용한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="ETLwithLLM 외부 API 연동안 실행")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ws-id", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--project-config",
        default='{"extractType":"dla"}',
        help="가이드 p.29 prj_config JSON. 외부 키는 extractType(camelCase)",
    )
    parser.add_argument("--max-wait", type=float, default=1800)
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument(
        "--region-reading",
        choices=("off", "shadow"),
        default="off",
        help="검토 기본은 off. 농협 VLM 계약 확정 뒤 shadow로 전환",
    )
    args = parser.parse_args()

    os.environ["REGION_READING_MODE"] = args.region_reading
    from nh_parser.integrations.etlwithllm import (
        EtlWithLlmClient,
        build_artifacts_from_dla,
    )

    config = json.loads(args.project_config)
    client = EtlWithLlmClient(args.base_url, args.ws_id, args.author)
    document = client.analyze_file(
        args.input,
        project_config=config,
        max_wait_s=args.max_wait,
        poll_interval_s=args.poll_interval,
    )

    raw_path = args.out / "etl-default" / f"{args.input.stem}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = build_artifacts_from_dla(args.input, document, args.out, preview=True)
    print(f"Default JSON: {raw_path}")
    print(f"parse: {artifacts.parse}")
    print(f"P1 evidence: {artifacts.evidence}")
    print(f"P3 review-input: {artifacts.review_input}")


if __name__ == "__main__":
    main()
