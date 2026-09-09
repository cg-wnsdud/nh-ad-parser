"""농협 AWX Custom Parser 비동기 API의 현재 저장소용 선택적 진입점."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import uuid as uuid_lib
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .service import configured_backend, run_ad_job
from .status import DONE, ERROR, PARSING, read_status, write_status


AdWorker = Callable[[str, str, str, dict[str, Any] | None], None]
_UUID = re.compile(r"^[0-9a-f]{32}$")
_RESULT_SUFFIXES = ("_parsed.json", "_review_input.json", "ad_summary.json")


def create_app(
    *,
    worker: AdWorker | None = None,
    work_root: Path | None = None,
    keep_work_dir: bool | None = None,
    timeout_s: int | None = None,
) -> FastAPI:
    """의존성을 주입할 수 있는 앱 팩토리. 테스트에서는 모델 호출 없는 worker를 쓴다."""
    active_worker = worker or run_ad_job
    root = Path(work_root) if work_root is not None else _default_work_root()
    keep = _env_bool("KEEP_WORK_DIR") if keep_work_dir is None else keep_work_dir
    timeout = int(os.getenv("TIMEOUT", "1800")) if timeout_s is None else timeout_s
    app = FastAPI(title="NH KL Custom Parser - nh-ad-parser", version="0.2.0")

    @app.post(
        "/parsing",
        name="Request RAG document parsing",
        description="현재 광고 전용 저장소에는 일반 문서 RAG/KL exporter가 없습니다.",
    )
    async def request_document_parsing():
        return JSONResponse(
            status_code=501,
            content={
                "message": (
                    "이 저장소는 광고 파싱 전용입니다. 일반 문서 /parsing은 기존 "
                    "nh_parsing.rag_ingest/kl_export 이식 범위를 별도로 확정해야 합니다."
                )
            },
        )

    @app.post("/ad/parsing", name="Request ad parsing")
    async def request_ad_parsing(
        background_tasks: BackgroundTasks,
        src_file: UploadFile = File(...),
        option: str | None = Form(default=None),
    ):
        job_id = uuid_lib.uuid4().hex
        work = root / job_id
        images = work / "image"
        images.mkdir(parents=True, exist_ok=False)
        filename = _safe_filename(src_file.filename)
        source = work / filename
        try:
            source.write_bytes(await src_file.read())
            write_status(work, PARSING)
            background_tasks.add_task(
                active_worker,
                str(work),
                str(images),
                str(source),
                _decode_option(option),
            )
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return JSONResponse(
            status_code=202,
            content={"result": "OK", "body": {"uuid": job_id, "timeout": timeout}},
        )

    @app.get("/parsing/result/{uuid}", name="Return parsing result")
    async def get_parsing_result(uuid: str):
        if not _UUID.fullmatch(uuid):
            return JSONResponse(
                status_code=500,
                content={"message": "Requested url does not exist."},
            )
        work = root / uuid
        if not work.is_dir():
            return JSONResponse(
                status_code=500,
                content={"message": "Requested url does not exist."},
            )
        status, message = read_status(work)
        if status == PARSING:
            return {"status": PARSING}
        if status == ERROR:
            return {"status": ERROR, "message": message}
        if status != DONE:
            return JSONResponse(
                status_code=500, content={"message": message or status},
            )
        try:
            payload = _build_result_zip(work)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"message": f"결과 ZIP 생성 실패: {exc}"},
            )
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
        return StreamingResponse(
            iter([payload]),
            media_type="application/x-zip-compressed",
            headers={"Content-Disposition": "attachment; filename=kl-core-s2-output.zip"},
        )

    @app.get("/health")
    async def health():
        try:
            backend = configured_backend()
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "ERROR", "message": str(exc)},
            )
        return {"status": "OK", "ad_backend": backend}

    return app


def _default_work_root() -> Path:
    configured = os.getenv("PATH_TEMP", "").strip()
    if configured:
        return Path(configured)
    date = datetime.now().astimezone().strftime("%Y-%m-%d")
    return Path("out") / date / "kl-parser-runtime"


def _decode_option(option: str | None) -> dict[str, Any] | None:
    if not option:
        return None
    try:
        value = json.loads(base64.b64decode(option, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_filename(value: str | None) -> str:
    name = Path(value or "").name.strip()
    return name if name not in {"", ".", ".."} else "upload.bin"


def _build_result_zip(work: Path) -> bytes:
    targets = [
        path
        for path in sorted(work.iterdir())
        if path.is_file() and path.name.endswith(_RESULT_SUFFIXES)
    ]
    parsed = next((p for p in targets if p.name.endswith("_parsed.json")), None)
    image_dir = work / "image"
    if parsed and image_dir.is_dir():
        images = [path for path in sorted(image_dir.iterdir()) if path.is_file()]
        if images:
            base = parsed.name.removesuffix("_parsed.json")
            image_zip = work / f"{base}_img.zip"
            with zipfile.ZipFile(image_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in images:
                    archive.write(path, path.name)
            targets.append(image_zip)
    if not any(path.name.endswith("_parsed.json") for path in targets):
        raise FileNotFoundError("광고 parsed 결과가 없습니다")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in targets:
            archive.write(path, path.name)
    return buffer.getvalue()


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


app = create_app()
