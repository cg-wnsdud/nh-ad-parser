"""가이드 1.16~1.20의 공개 API만 사용하는 ETLwithLLM 클라이언트.

로그인과 워크스페이스 생성은 농협 측 준비 범위이므로 구현하지 않는다. 이미 인증된
``requests.Session``과 전달받은 ``ws_id``를 주입받는다.
"""

from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from ...ocr.etlwithllm import normalize_default_document


PUBLIC_EXTRACT_TYPES = frozenset({"all", "dla", "parser"})
_NON_DEFAULT_SUFFIXES = (
    "_pages.json",
    "_page_grouped.json",
    "_chunk_data.json",
    "_edit.json",
)


class EtlApiError(RuntimeError):
    """HTTP 또는 ETL 응답 계약이 정상 처리 흐름과 다를 때 발생한다."""


@dataclass(frozen=True)
class EtlSubmission:
    task_id: str | None
    file_path: str


class EtlWithLlmClient:
    """파일 단위 비동기 ETL API 어댑터.

    ``CUSTOMIZE``는 의도적으로 허용하지 않는다. p.29 외부 API 표에 공개된 값은
    ``all/dla/parser``뿐이고, p.53의 내부 enum ``CUSTOMIZE``와의 매핑은 문서에 없다.
    """

    def __init__(
        self,
        base_url: str,
        ws_id: str,
        author: str,
        *,
        session: requests.Session | None = None,
        request_timeout_s: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.ws_id = ws_id
        self.author = author
        self.session = session or requests.Session()
        self.request_timeout_s = request_timeout_s

    def submit_files(
        self,
        paths: Iterable[Path],
        *,
        project_config: dict[str, Any] | None = None,
        result_types: str | list[str] = "default",
        callback_url: str | None = None,
        meta_info: dict[str, Any] | None = None,
    ) -> list[EtlSubmission]:
        inputs = [Path(path) for path in paths]
        if not inputs:
            raise ValueError("업로드할 파일이 없습니다")
        missing = [str(path) for path in inputs if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"업로드 파일이 없습니다: {missing}")

        config = dict(project_config or {})
        self._validate_project_config(config)
        tr_data: dict[str, Any] = {
            "author": self.author,
            "ws_id": self.ws_id,
            "res_type": result_types,
            "prj_config": config,
            "meta_info": dict(meta_info or {}),
        }
        if callback_url:
            tr_data["callback_url"] = callback_url

        streams = []
        try:
            files = []
            for path in inputs:
                stream = path.open("rb")
                streams.append(stream)
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files.append(("upfiles", (path.name, stream, mime)))
            response = self.session.post(
                f"{self.base_url}/api/v1/etl/auto/start",
                data={"tr_data": json.dumps(tr_data, ensure_ascii=False)},
                files=files,
                timeout=self.request_timeout_s,
            )
        finally:
            for stream in streams:
                stream.close()

        data = self._response_data(response, "분석 요청")
        if not isinstance(data, dict):
            raise EtlApiError("분석 요청 응답의 data가 object가 아닙니다")
        task_ids = list(data.get("task_ids") or [])
        file_paths = list(data.get("file_paths") or [])
        if not file_paths:
            raise EtlApiError("분석 요청 응답에 file_paths가 없습니다")
        if task_ids and len(task_ids) != len(file_paths):
            raise EtlApiError(
                f"task_ids와 file_paths 개수가 다릅니다: {len(task_ids)} != {len(file_paths)}"
            )
        return [
            EtlSubmission(task_ids[i] if i < len(task_ids) else None, str(file_path))
            for i, file_path in enumerate(file_paths)
        ]

    def status(self, file_path: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/api/v1/file/info",
            params={"file_path": file_path},
            timeout=self.request_timeout_s,
        )
        data = self._response_data(response, "상태 조회")
        sources = data.get("datasource") if isinstance(data, dict) else None
        if not isinstance(sources, list) or not sources or not isinstance(sources[0], dict):
            raise EtlApiError("상태 조회 응답에 data.datasource[0]이 없습니다")
        return sources[0]

    def wait_until_done(
        self,
        file_path: str,
        *,
        max_wait_s: float = 1800.0,
        poll_interval_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_s
        while True:
            state = self.status(file_path)
            chunk_status = str(state.get("chunk_status") or "")
            if chunk_status == "002":
                return state
            if chunk_status == "999":
                raise EtlApiError(f"ETL 분석 실패(chunk_status=999): {state}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"ETL 분석 대기 시간 초과: {file_path}")
            sleep(poll_interval_s)

    def list_results(self, file_path: str) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/api/v1/file/result/list",
            params={"docPath": file_path},
            timeout=self.request_timeout_s,
        )
        data = self._response_data(response, "결과 목록 조회")
        if not isinstance(data, list):
            raise EtlApiError("결과 목록 응답의 data가 list가 아닙니다")
        return [item for item in data if isinstance(item, dict)]

    def fetch_result(self, result_path: str) -> Any:
        response = self.session.get(
            f"{self.base_url}/api/v1/file/result/doc",
            params={"docResultPath": result_path},
            timeout=self.request_timeout_s,
        )
        self._raise_http(response, "결과 내용 조회")
        try:
            payload = response.json()
        except ValueError as exc:
            raise EtlApiError("결과 내용이 JSON이 아닙니다") from exc
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            return self._response_data(response, "결과 내용 조회")
        return payload

    def fetch_default_document(self, file_path: str, *, source_name: str) -> dict:
        entries = self.list_results(file_path)
        result_path = self._select_default_result(entries, source_name)
        return normalize_default_document(self.fetch_result(result_path))

    def analyze_file(
        self,
        path: Path,
        *,
        project_config: dict[str, Any] | None = None,
        max_wait_s: float = 1800.0,
        poll_interval_s: float = 5.0,
    ) -> dict:
        submission = self.submit_files(
            [path], project_config=project_config, result_types="default",
        )[0]
        self.wait_until_done(
            submission.file_path,
            max_wait_s=max_wait_s,
            poll_interval_s=poll_interval_s,
        )
        return self.fetch_default_document(submission.file_path, source_name=Path(path).name)

    @staticmethod
    def _validate_project_config(config: dict[str, Any]) -> None:
        if "extract_type" in config:
            raise ValueError(
                "외부 JSON 키는 extract_type이 아니라 가이드 p.29의 extractType입니다"
            )
        extract_type = config.get("extractType", "dla")
        if extract_type not in PUBLIC_EXTRACT_TYPES:
            raise ValueError(
                f"공개 API에서 확인되지 않은 extractType={extract_type!r}; "
                f"허용값은 {sorted(PUBLIC_EXTRACT_TYPES)}"
            )

    @staticmethod
    def _select_default_result(entries: list[dict[str, Any]], source_name: str) -> str:
        source = Path(source_name)
        expected_names = {f"{source.stem}.json", f"{source.name}.json"}
        candidates = []
        exact = []
        for entry in entries:
            name = str(entry.get("file_name") or "")
            path = str(entry.get("file_path") or "")
            if not path or not name.endswith(".json") or name.endswith(_NON_DEFAULT_SUFFIXES):
                continue
            candidates.append(path)
            if name in expected_names:
                exact.append(path)
        if len(exact) == 1:
            return exact[0]
        if len(candidates) == 1:
            return candidates[0]
        raise EtlApiError(
            "Default JSON 결과를 하나로 결정할 수 없습니다: "
            f"source={source_name!r}, candidates={candidates!r}"
        )

    @classmethod
    def _response_data(cls, response: Any, operation: str) -> Any:
        cls._raise_http(response, operation)
        try:
            payload = response.json()
        except ValueError as exc:
            raise EtlApiError(f"{operation} 응답이 JSON이 아닙니다") from exc
        if not isinstance(payload, dict):
            raise EtlApiError(f"{operation} 응답이 object가 아닙니다")
        result = payload.get("result")
        if isinstance(result, dict):
            code = result.get("code")
            if str(code) != "0":
                raise EtlApiError(f"{operation} 실패: code={code}, message={result.get('message')}")
        elif "detail" in payload:
            raise EtlApiError(f"{operation} 실패: detail={payload['detail']}")
        return payload.get("data")

    @staticmethod
    def _raise_http(response: Any, operation: str) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            body = str(getattr(response, "text", ""))[:500]
            raise EtlApiError(f"{operation} HTTP {status}: {body}")
