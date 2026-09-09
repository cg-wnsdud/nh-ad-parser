# -*- coding: utf-8 -*-
"""기존 보고용 Knowledge Lake 광고 API 프로토타입을 실행한다."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "KL API 의존성이 없습니다. uv sync --extra kl-api 를 먼저 실행하세요."
        ) from exc
    uvicorn.run(
        "nh_parser.kl_api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
