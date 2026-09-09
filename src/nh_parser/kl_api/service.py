"""AWX API 작업을 현재 광고 파이프라인 또는 ETLwithLLM A안에 연결한다."""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Any

import requests

from ..review import build_evidence, build_review_input, label_regions, resolve_template
from .status import DONE, ERROR, PARSING, write_status


BACKEND_LOCAL = "local"
BACKEND_ETL = "etl"
SUPPORTED_BACKENDS = frozenset({BACKEND_LOCAL, BACKEND_ETL})


class KlBackendConfigError(RuntimeError):
    """선택한 광고 파싱 백엔드의 필수 설정이 없거나 잘못됐을 때 발생한다."""


def configured_backend() -> str:
    value = os.getenv("NH_KL_AD_BACKEND", BACKEND_LOCAL).strip().lower()
    if value not in SUPPORTED_BACKENDS:
        raise KlBackendConfigError(
            f"NH_KL_AD_BACKEND={value!r}; 허용값은 {sorted(SUPPORTED_BACKENDS)}"
        )
    return value


def run_ad_job(
    work_dir: str | Path,
    image_dir: str | Path,
    file_path: str | Path,
    option: dict[str, Any] | None = None,
) -> None:
    """광고 파일 하나를 처리하고 AWX 상태 및 반환 대상 파일을 기록한다."""
    work = Path(work_dir)
    images = Path(image_dir)
    source = Path(file_path)
    try:
        write_status(work, PARSING)
        backend = configured_backend()
        if backend == BACKEND_LOCAL:
            document = _process_local(source, images)
        else:
            document = _process_etl(source, images)
        _write_ad_outputs(work, source, document, backend=backend, option=option)
        write_status(work, DONE)
    except Exception as exc:  # 백그라운드 작업 실패를 폴링 응답으로 전달한다.
        traceback.print_exc()
        write_status(work, ERROR, f"{exc.__class__.__name__}: {exc}")


def _process_local(source: Path, image_dir: Path):
    """기존 DGX PaddleX/Gemma 경로. 개발 회귀 및 비교용 백엔드다."""
    from ..pipeline import process_file

    image_dir.mkdir(parents=True, exist_ok=True)
    return process_file(source, preview_dir=image_dir)


def _process_etl(source: Path, image_dir: Path):
    """농협 내부 ETL HTTP API로 DLA를 요청한 뒤 공통 후처리 경계로 연결한다."""
    from ..integrations.etlwithllm import EtlWithLlmClient
    from ..integrations.etlwithllm.pipeline import process_file_from_dla

    base_url = _required_env("ETL_BASE_URL")
    ws_id = _required_env("ETL_WS_ID")
    author = _required_env("ETL_AUTHOR")
    project_config = _json_env(
        "ETL_PROJECT_CONFIG", default={"extract_type": "dla"},
    )
    session = _build_etl_session()
    client = EtlWithLlmClient(
        base_url,
        ws_id,
        author,
        session=session,
        request_timeout_s=float(os.getenv("ETL_REQUEST_TIMEOUT_S", "120")),
    )
    payload = client.analyze_file(
        source,
        project_config=project_config,
        max_wait_s=float(os.getenv("ETL_MAX_WAIT_S", "1800")),
        poll_interval_s=float(os.getenv("ETL_POLL_INTERVAL_S", "5")),
    )
    image_dir.mkdir(parents=True, exist_ok=True)
    return process_file_from_dla(source, payload, preview_dir=image_dir)


def _build_etl_session() -> requests.Session:
    """농협이 제공한 인증 상태만 주입한다. 계정/비밀번호 로그인은 수행하지 않는다."""
    session = requests.Session()
    token = os.getenv("ETL_TOKEN", "").strip()
    if token:
        # 가이드 1.1 로그인 응답은 Set-Cookie의 token으로 인증 상태를 전달한다.
        session.cookies.set("token", token)
    authorization = os.getenv("ETL_AUTHORIZATION", "").strip()
    if authorization:
        # 이 헤더는 가이드 기본 계약이 아니라 별도 게이트웨이 환경을 위한 선택값이다.
        session.headers["Authorization"] = authorization
    return session


def _write_ad_outputs(
    work: Path,
    source: Path,
    document: Any,
    *,
    backend: str,
    option: dict[str, Any] | None,
) -> None:
    """현재 P1/P3를 기존 보고의 ``*_parsed``/``*_review_input`` 이름으로 내보낸다."""
    parsed = document.model_dump(mode="json")
    resolution = resolve_template(parsed)
    labeling = label_regions(parsed, resolution)
    evidence = build_evidence(parsed, resolution, labeling)
    review_input = build_review_input(evidence)

    parsed_path = work / f"{source.name}_parsed.json"
    review_path = work / f"{source.name}_review_input.json"
    _write_json(parsed_path, evidence)
    _write_json(review_path, review_input)
    _write_json(
        work / "ad_summary.json",
        {
            "doc_id": evidence.get("doc_id"),
            "file_type": evidence.get("file_type"),
            "classification": evidence.get("classification"),
            "template": {
                key: (evidence.get("template") or {}).get(key)
                for key in ("template_id", "status", "source", "confidence", "reason")
            },
            "page_count": len(evidence.get("pages") or []),
            "region_count": (evidence.get("summary") or {}).get("region_count"),
            "line_count": (evidence.get("summary") or {}).get("line_count"),
            "review_input_summary": review_input.get("summary"),
            "parser_backend": backend,
            "request_option": option,
            "notes": evidence.get("notes") or [],
        },
    )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise KlBackendConfigError(f"{name} 환경변수가 필요합니다")
    return value


def _json_env(name: str, *, default: dict[str, Any]) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return dict(default)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KlBackendConfigError(f"{name}이 유효한 JSON이 아닙니다: {exc}") from exc
    if not isinstance(value, dict):
        raise KlBackendConfigError(f"{name}은 JSON object여야 합니다")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(path)
