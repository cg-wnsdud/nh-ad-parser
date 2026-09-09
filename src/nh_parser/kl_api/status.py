"""농협 Custom Parser 예제의 ``genaikl.status`` 파일 계약."""

from __future__ import annotations

import json
from pathlib import Path


STATUS_FILENAME = "genaikl.status"
PARSING = "PARSING"
DONE = "DONE"
ERROR = "ERROR"


def write_status(work_dir: Path, status: str, message: str = "") -> None:
    """폴링 중 불완전한 JSON을 읽지 않도록 상태 파일을 원자적으로 교체한다."""
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / STATUS_FILENAME
    temporary = root / f".{STATUS_FILENAME}.tmp"
    temporary.write_text(
        json.dumps({"status": status, "message": message}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(destination)


def read_status(work_dir: Path) -> tuple[str, str]:
    root = Path(work_dir)
    destination = root / STATUS_FILENAME
    if not destination.is_file():
        temporary = root / f".{STATUS_FILENAME}.tmp"
        if root.is_dir() and temporary.exists():
            return PARSING, ""
        return "UNKNOWN", "Requested url does not exist."
    data = json.loads(destination.read_text(encoding="utf-8"))
    return str(data["status"]), str(data.get("message") or "")
