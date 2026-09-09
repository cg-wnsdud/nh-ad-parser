#!/usr/bin/env python3
"""농협 Jupyter/폐쇄망 실행 환경 사전점검 - 표준 라이브러리만 사용한다.

패키지가 아직 설치되지 않은 환경에서도 Python 버전, 필요한 환경변수, 쓰기 권한,
ETL/vLLM 호스트의 TCP 접근 가능 여부를 확인할 수 있어야 하므로 외부 의존성이 없다.
비밀번호나 토큰 값은 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


BASE_IMPORTS = {
    "nh_parser": "현재 저장소 패키지",
    "requests": "HTTP 클라이언트",
    "pydantic": "데이터 모델",
    "PIL": "이미지 처리(Pillow)",
    "pypdfium2": "PDF 렌더링",
}
KL_IMPORTS = {
    "fastapi": "KL/AWX FastAPI",
    "uvicorn": "API 서버",
    "multipart": "multipart 업로드",
}
ETL_ENV = ("ETL_BASE_URL", "ETL_WS_ID", "ETL_AUTHOR")
VLM_ENV = ("GEMMA_URL", "GEMMA_MODEL")


def _load_env_file(path: Path) -> None:
    """프로젝트와 같은 규칙으로 .env의 아직 없는 값만 읽는다."""
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


def _check_imports(include_kl: bool) -> list[str]:
    failures: list[str] = []
    targets = dict(BASE_IMPORTS)
    if include_kl:
        targets.update(KL_IMPORTS)
    print("[패키지]")
    for module, purpose in targets.items():
        ok = importlib.util.find_spec(module) is not None
        print(f"  {'OK' if ok else 'MISSING':7s} {module:12s} - {purpose}")
        if not ok:
            failures.append(f"패키지 없음: {module}")
    return failures


def _check_environment() -> list[str]:
    failures: list[str] = []
    print("[환경변수]")
    for name in (*ETL_ENV, *VLM_ENV):
        configured = bool(os.getenv(name, "").strip())
        print(f"  {'OK' if configured else 'MISSING':7s} {name}")
        if not configured:
            failures.append(f"환경변수 없음: {name}")
    print(f"  {'SET' if os.getenv('ETL_TOKEN', '').strip() else 'OPTION':7s} ETL_TOKEN")
    return failures


def _check_write_dir(path: Path) -> list[str]:
    print("[작업 디렉터리]")
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".nh-runtime-check-", dir=path, delete=True):
            pass
    except OSError as exc:
        print(f"  FAIL    {path} - {exc}")
        return [f"작업 디렉터리 쓰기 실패: {path}"]
    print(f"  OK      {path}")
    return []


def _default_port(parsed) -> int | None:
    if parsed.port:
        return parsed.port
    if parsed.scheme == "http":
        return 80
    if parsed.scheme == "https":
        return 443
    return None


def _check_tcp(name: str, url: str, timeout: float) -> list[str]:
    parsed = urlparse(url)
    port = _default_port(parsed)
    if not parsed.hostname or port is None:
        print(f"  FAIL    {name} - URL 형식/포트 확인 필요")
        return [f"URL 확인 필요: {name}"]
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            pass
    except OSError as exc:
        print(f"  FAIL    {name} {parsed.hostname}:{port} - {exc}")
        return [f"TCP 연결 실패: {name}"]
    print(f"  OK      {name} {parsed.hostname}:{port}")
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="농협 A안/Jupyter 실행 환경 사전점검")
    parser.add_argument(
        "--mode", choices=("direct", "kl"), default="kl",
        help="direct=A안 CLI만, kl=KL/AWX FastAPI까지 점검",
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path(".env"),
        help="확인할 환경 설정 파일. 이미 export된 값이 우선",
    )
    parser.add_argument(
        "--work-dir", type=Path,
        default=Path(os.getenv("PATH_TEMP", "out/runtime-check")),
    )
    parser.add_argument(
        "--network", action="store_true",
        help="ETL_BASE_URL과 GEMMA_URL 호스트의 TCP 접근도 확인",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()
    _load_env_file(args.env_file)

    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    failures: list[str] = []
    if sys.version_info < (3, 13):
        failures.append("Python 3.13 미만: 현재 pyproject.toml 요구조건과 불일치")
        print("  FAIL    현재 패키지는 Python 3.13 이상 필요")
    else:
        print("  OK      Python 3.13 이상")

    failures.extend(_check_imports(args.mode == "kl"))
    failures.extend(_check_environment())
    failures.extend(_check_write_dir(args.work_dir))

    if args.network:
        print("[TCP 접근]")
        for name in ("ETL_BASE_URL", "GEMMA_URL"):
            value = os.getenv(name, "").strip()
            if value:
                failures.extend(_check_tcp(name, value, args.timeout))
            else:
                print(f"  SKIP    {name} - 값 없음")

    if failures:
        print(f"\n결과: FAIL ({len(failures)}건)")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\n결과: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
