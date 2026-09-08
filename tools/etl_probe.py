# -*- coding: utf-8 -*-
"""농협 폐쇄망 현장 점검용 ETL with LLM 프로브 — **의존성 0, 단독 실행**.

규격: [docs/농협-ocr-연동-규격.md](../docs/농협-ocr-연동-규격.md)
현장 절차·체크리스트: [docs/농협-ocr-현장점검.md](../docs/농협-ocr-현장점검.md)

## 왜 표준 라이브러리만 쓰나

농협 폐쇄망 안에서만 이 API 를 호출할 수 있고, 그 안에서는 `pip install`·`uv sync`
가 안 된다. `requests` 조차 없다고 가정해야 안전하다. 그래서 multipart 본문을 손으로
조립하고 `urllib` 로 보낸다. **이 파일 하나만 USB 로 들고 가면 돈다** (Python 3.9+).

## 왜 단계를 쪼개 두나

현장 시간이 짧고 왕복이 비싸다. 3단계에서 막혔을 때 1·2단계를 다시 돌리지 않아야
한다. 그래서 각 단계가 결과를 `state.json` 에 남기고 다음 단계가 그것을 읽는다.

## 무엇을 반드시 남기나

**모든 요청·응답을 파일로 저장한다.** 현장에서 얻을 수 있는 가장 값진 산출물은
"원본 응답 JSON" 이다. 그것만 있으면 변환·영역조립·비교를 전부 사무실에서 다시
할 수 있다. 화면에만 찍고 나오면 다시 와야 한다.

## 사용법

    python tools/etl_probe.py login   --base-url http://IP:58000 --user-id U --user-pw P
    python tools/etl_probe.py ws-list --base-url http://IP:58000 --user-id U
    python tools/etl_probe.py run     --base-url http://IP:58000 --ws-id WS... \
                                      --author me --input a.png b.pdf
    python tools/etl_probe.py summary                 # 네트워크 없이 계약 점검

`run` 은 submit → poll → results → fetch 를 한 번에 한다. 단계별로 하려면 같은 이름의
서브커맨드를 따로 부르면 된다. 산출물은 `--out`(기본 `probe-out/<타임스탬프>`) 아래.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path


# Windows 폐쇄망 PC의 기본 CP949 콘솔에서도 한글·기호 출력 때문에 실행이 멈추지 않게 한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_TIMEOUT_S = 120
# 폴링 간격. 문서에 권장 주기가 없어 짧게 시작해 늘린다(서버를 두드려 패지 않도록).
POLL_INTERVALS_S = [2, 2, 3, 3, 5, 5, 5, 10, 10, 10, 15, 15, 20, 20, 30]

CHUNK_STATUS = {
    "000": "STATUS_INIT (초기)",
    "001": "STATUS_PROGRESS (분석 진행중)",
    "002": "STATUS_DONE (분석 완료)",
    "999": "STATUS_ERROR (분석 오류)",
}
CHUNK_STEP = {
    "000": "STEP_READY (준비)",
    "001": "STEP_EXTRACT (파일 추출)",
    "002": "STEP_TRANSFORM (파일 변환)",
}


# ─────────────────────────────── 저장소 ───────────────────────────────

class Run:
    """산출물 디렉터리 + `state.json`. 단계 사이의 유일한 연결 고리다."""

    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists() else {}
        self._seq = len(list(self.dir.glob("[0-9][0-9]_*")))

    def save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8"
        )

    def record(self, name: str, payload) -> Path:
        """단계 산출물을 순번 붙여 저장한다. 순번이 곧 현장에서의 진행 순서다."""
        path = self.dir / f"{self._seq:02d}_{name}.json"
        self._seq += 1
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_text(text, "utf-8")
        print(f"    ↳ 저장 {path}")
        return path


def latest_run(base: Path) -> Path:
    """가장 최근 run 디렉터리. `summary` 를 인자 없이 부를 수 있게 한다."""
    runs = sorted(p for p in base.glob("*") if p.is_dir())
    if not runs:
        sys.exit(f"run 디렉터리가 없다: {base}")
    return runs[-1]


# ─────────────────────────────── HTTP ───────────────────────────────

def _request(method: str, url: str, *, body: bytes | None = None,
             headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> tuple[int, dict, str]:
    """요청 1회. **에러 본문도 반드시 돌려준다.**

    urllib 는 4xx/5xx 에서 예외를 던지는데, 이 API 는 에러 본문에 진짜 정보를 담아
    보낸다(FastAPI `detail` / 플랫폼 `result.code`). 예외로 날려버리면 현장에서
    원인을 못 본다.
    """
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, {}, f"__URLError__ {exc.reason}"


def _multipart(fields: dict[str, str], files: list[Path]) -> tuple[bytes, str]:
    """multipart/form-data 본문을 손으로 조립한다 (`upfiles` 는 같은 이름 반복).

    파일 MIME 은 확장자로 추측한다 — 공식 샘플은 `application/pdf` 로 고정해 두는데,
    그러면 PNG 를 올렸을 때 서버가 거부한 것인지 MIME 때문인지 갈릴 수 없다.
    `--force-pdf-mime` 로 공식 샘플과 같게 맞춰 두 경우를 갈라 볼 수 있다.
    """
    boundary = f"----etlprobe{uuid.uuid4().hex}"
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if os.environ.get("ETL_PROBE_FORCE_PDF_MIME") == "1":
            mime = "application/pdf"
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="upfiles"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        out += path.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _auth_headers(run: Run) -> dict:
    token = run.state.get("token")
    return {"Cookie": f"token={token}"} if token else {}


def _show(status: int, text: str, limit: int = 400) -> object:
    """상태·본문 요지를 찍고 파싱된 JSON(또는 원문)을 돌려준다."""
    print(f"    HTTP {status}")
    try:
        data = json.loads(text)
    except ValueError:
        print(f"    (JSON 아님) {text[:limit]}")
        return text
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict):
        code, msg = result.get("code"), str(result.get("message"))
        print(f"    result.code={code}  message={msg[:limit]}")
    if isinstance(data, dict) and "detail" in data:
        # FastAPI 원형 에러. 플랫폼 표준형과 모양이 다르다(규격 §9.3).
        print(f"    detail={json.dumps(data['detail'], ensure_ascii=False)[:limit]}")
    return data


# ─────────────────────────────── 단계 ───────────────────────────────

def cmd_login(args, run: Run) -> None:
    url = f"{args.base_url}/api/v1/user/login"
    body = json.dumps({"user_id": args.user_id, "user_pw": args.user_pw}).encode()
    print(f"[login] POST {url}")
    status, headers, text = _request(
        "POST", url, body=body, headers={"Content-Type": "application/json"}
    )
    data = _show(status, text)
    run.record("login", data)

    # 토큰은 Set-Cookie 로 온다(응답 본문에는 없다).
    cookie = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
    token = None
    for part in cookie.split(";"):
        if part.strip().startswith("token="):
            token = part.strip()[len("token="):]
            break
    if token:
        run.state["token"] = token
        run.save_state()
        print(f"    토큰 확보 (길이 {len(token)})")
    else:
        print("    ⚠ Set-Cookie 에 token 이 없다. 인증 미들웨어가 꺼져 있을 수 있다")
        print(f"    Set-Cookie: {cookie[:200] or '(없음)'}")


def cmd_ws_list(args, run: Run) -> None:
    url = f"{args.base_url}/api/v1/workspace/list"
    body = json.dumps({"search": args.search, "user_id": args.user_id}).encode()
    print(f"[ws-list] POST {url}")
    status, _, text = _request(
        "POST", url, body=body,
        headers={"Content-Type": "application/json", **_auth_headers(run)},
    )
    data = _show(status, text)
    run.record("ws_list", data)
    for ws in (data.get("data") or []) if isinstance(data, dict) else []:
        if isinstance(ws, dict):
            print(f"      {ws.get('id')}  {ws.get('workspace_name')}  "
                  f"access={ws.get('access_level')}  files={ws.get('source_cnt')}")


def cmd_ws_create(args, run: Run) -> None:
    url = f"{args.base_url}/api/v1/workspace"
    body = json.dumps({
        "workspace_name": args.name,
        "description": args.description,
        "access_level": args.access_level,
        "created_by": args.created_by,
        "path": args.name,
    }).encode()
    print(f"[ws-create] POST {url}")
    status, _, text = _request(
        "POST", url, body=body,
        headers={"Content-Type": "application/json", **_auth_headers(run)},
    )
    data = _show(status, text)
    run.record("ws_create", data)
    ws_id = ((data.get("data") or {}).get("id")) if isinstance(data, dict) else None
    if ws_id:
        run.state["ws_id"] = ws_id
        run.save_state()
        print(f"    ws_id = {ws_id}")


def cmd_submit(args, run: Run) -> None:
    inputs = [Path(p) for p in args.input]
    missing = [p for p in inputs if not p.is_file()]
    if missing:
        sys.exit(f"입력 파일이 없다: {missing}")

    tr_data = {"ws_id": args.ws_id or run.state.get("ws_id")}
    if not tr_data["ws_id"]:
        sys.exit("ws_id 가 없다 — --ws-id 를 주거나 ws-create 를 먼저 실행할 것")
    # 표는 author 를 필수라 하고 공식 샘플에는 없다(규격 §9.1 M6).
    # --omit-author 로 실제로 필수인지 현장에서 확인한다.
    if not args.omit_author:
        tr_data["author"] = args.author
    if args.callback_url:
        tr_data["callback_url"] = args.callback_url
    tr_data["res_type"] = args.res_type
    tr_data["prj_config"] = json.loads(args.prj_config) if args.prj_config else {}
    tr_data["meta_info"] = {"probe": "nh-ad-parser", "ts": datetime.now().isoformat()}

    body, content_type = _multipart({"tr_data": json.dumps(tr_data, ensure_ascii=False)}, inputs)
    url = f"{args.base_url}/api/v1/etl/auto/start"
    print(f"[submit] POST {url}")
    print(f"    tr_data = {json.dumps(tr_data, ensure_ascii=False)}")
    print(f"    upfiles = {[p.name for p in inputs]}  ({len(body)/1024:.0f} KB)")
    run.record("submit_request", {"url": url, "tr_data": tr_data,
                                  "files": [p.name for p in inputs]})

    status, _, text = _request(
        "POST", url, body=body,
        headers={"Content-Type": content_type, "Accept": "application/json", **_auth_headers(run)},
    )
    data = _show(status, text)
    run.record("submit_response", data)

    payload = (data.get("data") or {}) if isinstance(data, dict) else {}
    file_paths = payload.get("file_paths") or []
    if file_paths:
        run.state["file_paths"] = file_paths
        run.state["task_ids"] = payload.get("task_ids") or []
        run.save_state()
        for fp in file_paths:
            print(f"      file_path = {fp}")
    else:
        print("    ⚠ file_paths 가 없다. 위 응답을 그대로 기록해 두고 원인을 확인할 것")


def cmd_poll(args, run: Run) -> None:
    for file_path in _target_paths(args, run):
        print(f"[poll] {file_path}")
        for i, wait in enumerate(POLL_INTERVALS_S):
            url = (f"{args.base_url}/api/v1/file/info"
                   f"?file_path={urllib.parse.quote(file_path, safe='')}")
            status, _, text = _request("GET", url, headers=_auth_headers(run))
            data = json.loads(text) if text.startswith("{") else {}
            sources = ((data.get("data") or {}).get("datasource") or []) if data else []
            if not sources:
                _show(status, text)
                run.record("poll_error", data or text)
                break
            src = sources[0]
            cs, st = str(src.get("chunk_status")), str(src.get("chunk_step"))
            print(f"    [{i+1}] chunk_status={cs} {CHUNK_STATUS.get(cs,'?')}"
                  f" / chunk_step={st} {CHUNK_STEP.get(st,'?')}")
            if cs == "002":
                run.record("poll_done", data)
                break
            if cs == "999":
                print("    ⚠ 분석 오류(999). 결과 파일 목록의 pipeline_log.log 를 확인할 것")
                run.record("poll_failed", data)
                break
            time.sleep(wait)
        else:
            print("    ⚠ 폴링 한도 초과 — 아직 진행 중일 수 있다. poll 을 다시 실행할 것")


def cmd_results(args, run: Run) -> None:
    found: dict[str, list[str]] = {}
    for file_path in _target_paths(args, run):
        url = (f"{args.base_url}/api/v1/file/result/list"
               f"?docPath={urllib.parse.quote(file_path, safe='')}")
        print(f"[results] GET {url}")
        status, _, text = _request("GET", url, headers=_auth_headers(run))
        data = _show(status, text)
        run.record("result_list", data)
        paths = []
        for item in (data.get("data") or []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and item.get("file_path"):
                paths.append(item["file_path"])
                print(f"      {item.get('file_name')}  ←  {item['file_path']}")
        found[file_path] = paths
    run.state["result_paths"] = found
    run.save_state()


def cmd_fetch(args, run: Run) -> None:
    """결과 내용을 받아 **원문 그대로** 저장한다. 이게 현장의 핵심 산출물이다."""
    targets = []
    if args.result_path:
        targets = list(args.result_path)
    else:
        for paths in (run.state.get("result_paths") or {}).values():
            # 기본 JSON(`[파일명].json`)만 고른다. _pages/_page_grouped/log 는 제외.
            targets += [p for p in paths if p.endswith(".json") and "_" not in Path(p).stem]
    if not targets:
        sys.exit("받을 결과 경로가 없다 — results 를 먼저 실행하거나 --result-path 를 줄 것")

    raw_dir = run.dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    for result_path in targets:
        url = (f"{args.base_url}/api/v1/file/result/doc"
               f"?docResultPath={urllib.parse.quote(result_path, safe='')}")
        print(f"[fetch] GET {url}")
        status, _, text = _request("GET", url, headers=_auth_headers(run))
        print(f"    HTTP {status}  {len(text)/1024:.1f} KB")
        name = result_path.replace("/", "__")
        (raw_dir / name).write_text(text, "utf-8")
        print(f"    ↳ 저장 {raw_dir / name}")


def cmd_run(args, run: Run) -> None:
    cmd_submit(args, run)
    if not run.state.get("file_paths"):
        sys.exit("submit 이 file_paths 를 주지 않아 중단한다")
    cmd_poll(args, run)
    cmd_results(args, run)
    cmd_fetch(args, run)
    print("\n다음: python tools/etl_probe.py summary")


def _target_paths(args, run: Run) -> list[str]:
    if getattr(args, "file_path", None):
        return list(args.file_path)
    paths = run.state.get("file_paths") or []
    if not paths:
        sys.exit("file_path 가 없다 — submit 을 먼저 실행하거나 --file-path 를 줄 것")
    return paths


# ─────────────────────────── 계약 점검 (오프라인) ───────────────────────────

def cmd_summary(args, run: Run) -> None:
    """받아온 원응답으로 **계약**을 점검한다. 네트워크가 필요 없다.

    성능(회수율)이 아니라 "우리 하류가 기대하는 필드가 실제로 오는가" 를 본다.
    현장에서 5분 안에 판정할 수 있어야 하므로 항목을 좁게 고정했다.
    """
    raw_dir = run.dir / "raw"
    files = sorted(raw_dir.glob("*")) if raw_dir.exists() else []
    if not files:
        sys.exit(f"원응답이 없다: {raw_dir} (fetch 를 먼저 실행할 것)")

    for path in files:
        print(f"\n{'='*72}\n{path.name}")
        try:
            doc = json.loads(path.read_text("utf-8"))
        except ValueError as exc:
            print(f"  JSON 파싱 실패: {exc}")
            continue
        # 콜백 경로면 doc_result 안에 들어 있다. 깊이가 문서와 엇갈리므로(§9.1 M1) 둘 다 본다.
        if "pages" not in doc:
            inner = doc.get("doc_result") or {}
            doc = inner.get("default") or inner or doc
            print("  (doc_result 안에서 본문을 찾았다 — M1 확인 필요)")
        _summarize_doc(doc)


def _summarize_doc(doc: dict) -> None:
    pages = doc.get("pages") or []
    print(f"  pdfName={doc.get('pdfName')}  pageLen={doc.get('pageLen')}  pages={len(pages)}")

    types: dict[str, int] = {}
    n_lines = n_chars = 0
    line_conf = para_conf = 0
    bbox_shapes: dict[str, int] = {}
    table_stats = {"표": 0, "lines 있음": 0, "cells 있음": 0, "cell_info 있음": 0, "병합셀": 0}
    warnings: dict[str, int] = {}

    for page in pages:
        print(f"  - page {page.get('pageId')}: {page.get('width')} x {page.get('height')}"
              f"  paragraphs={len(page.get('paragraphs') or [])}")
        for w in page.get("warning_messages") or []:
            if isinstance(w, dict):
                warnings[str(w.get("code"))] = warnings.get(str(w.get("code")), 0) + 1
        for para in page.get("paragraphs") or []:
            if not isinstance(para, dict):
                continue
            t = str(para.get("type"))
            types[t] = types.get(t, 0) + 1
            if para.get("confidence") is not None:
                para_conf += 1
            bbox_shapes[_bbox_shape(para.get("bbox"))] = \
                bbox_shapes.get(_bbox_shape(para.get("bbox")), 0) + 1
            lines = para.get("lines") or []
            n_lines += len(lines)
            for ln in lines:
                if isinstance(ln, dict):
                    n_chars += len(str(ln.get("contents") or ""))
                    if ln.get("confidence") is not None:
                        line_conf += 1
            if t.lower() == "table":
                table_stats["표"] += 1
                table_stats["lines 있음"] += 1 if lines else 0
                cells = para.get("cells") or []
                table_stats["cells 있음"] += 1 if cells else 0
                infos = [c.get("cell_info") for c in cells if isinstance(c, dict)]
                table_stats["cell_info 있음"] += 1 if any(infos) else 0
                for info in infos:
                    if isinstance(info, (list, tuple)) and len(info) == 4:
                        if int(info[1]) > int(info[0]) or int(info[3]) > int(info[2]):
                            table_stats["병합셀"] += 1
                            break

    print(f"\n  문단 타입 분포: {types}")
    print(f"  bbox 형태: {bbox_shapes}   (기대: '폴리곤4점')")
    print(f"  줄 {n_lines}개 / 글자 {n_chars}자")
    print(f"  warning_messages: {warnings or '없음'}")

    print("\n  ── 계약 점검 ──")
    _verdict("문단 confidence 존재", para_conf > 0, "있음(정상)", "없음 — 영역 점수를 못 쓴다")
    _verdict("줄 confidence 존재", line_conf > 0,
             f"있음! {line_conf}건 — G1 해소, 저신뢰 재판독 유지 가능",
             "없음 — 예상대로 G1. 저신뢰 재판독 근거를 다른 것으로 바꿔야 한다")
    if table_stats["표"]:
        print(f"  표 {table_stats['표']}개: {table_stats}")
        _verdict("표에 줄(lines) 존재", table_stats["lines 있음"] > 0,
                 "있음! — G11 해소, 표 정본 정책 유지 가능",
                 "없음 — 예상대로 G11. 표 정본을 cells 로 올릴지 결정해야 한다")
        _verdict("표 cell_info 존재", table_stats["cell_info 있음"] > 0,
                 "있음(정상) — 행·열을 직접 쓸 수 있다", "없음 — HTML 파싱 폴백이 필요하다")
        if table_stats["병합셀"]:
            print("    ★ 병합셀이 있다 — cell_info 양끝포함 해석을 이 표로 검증할 것")
        else:
            print("    · 병합셀 없음 — span 해석은 아직 미검증이다")
    else:
        print("  ⚠ 표가 없는 입력이다. 표 포함 문서로 한 번 더 확인할 것")


def _bbox_shape(bbox) -> str:
    if not isinstance(bbox, (list, tuple)) or not bbox:
        return "없음"
    if isinstance(bbox[0], (list, tuple)):
        return f"폴리곤{len(bbox)}점"
    return f"평탄{len(bbox)}개"


def _verdict(label: str, ok: bool, yes: str, no: str) -> None:
    print(f"    [{'O' if ok else 'X'}] {label}: {yes if ok else no}")


# ─────────────────────────────── CLI ───────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="농협 ETL with LLM 현장 점검 프로브 (의존성 없음)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out", default="probe-out", help="산출물 상위 디렉터리 (기본 probe-out)")
    p.add_argument("--run-dir", help="이어 쓸 run 디렉터리를 직접 지정한다")
    p.add_argument("--new-run", action="store_true",
                   help="새 run 디렉터리에서 시작한다 (기본: 가장 최근 것을 이어 쓴다)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_base(sp, need_base_url=True):
        if need_base_url:
            sp.add_argument("--base-url", required=True, help="예: http://192.168.0.10:58000")
        return sp

    sp = add_base(sub.add_parser("login", help="로그인해 토큰 확보"))
    sp.add_argument("--user-id", required=True)
    sp.add_argument("--user-pw", required=True)
    sp.set_defaults(func=cmd_login)

    sp = add_base(sub.add_parser("ws-list", help="워크스페이스 목록"))
    sp.add_argument("--user-id", required=True)
    sp.add_argument("--search", default="")
    sp.set_defaults(func=cmd_ws_list)

    sp = add_base(sub.add_parser("ws-create", help="워크스페이스 생성"))
    sp.add_argument("--name", required=True)
    sp.add_argument("--created-by", required=True)
    sp.add_argument("--description", default="nh-ad-parser probe")
    sp.add_argument("--access-level", default="002", choices=["001", "002"])
    sp.set_defaults(func=cmd_ws_create)

    def add_submit_args(sp):
        sp.add_argument("--ws-id")
        sp.add_argument("--author", default="nh-ad-parser")
        sp.add_argument("--omit-author", action="store_true",
                        help="tr_data 에서 author 를 빼고 보낸다 (필수 여부 확인용)")
        sp.add_argument("--input", nargs="+", required=True, help="업로드할 파일")
        sp.add_argument("--res-type", default="default")
        sp.add_argument("--callback-url")
        sp.add_argument("--prj-config", help='예: \'{"dla_score_th":0.3}\'')
        return sp

    sp = add_submit_args(add_base(sub.add_parser("submit", help="업로드 + 분석 요청")))
    sp.set_defaults(func=cmd_submit)

    sp = add_base(sub.add_parser("poll", help="분석 상태 폴링"))
    sp.add_argument("--file-path", nargs="*")
    sp.set_defaults(func=cmd_poll)

    sp = add_base(sub.add_parser("results", help="결과 파일 목록"))
    sp.add_argument("--file-path", nargs="*")
    sp.set_defaults(func=cmd_results)

    sp = add_base(sub.add_parser("fetch", help="결과 내용 받아 저장"))
    sp.add_argument("--result-path", nargs="*")
    sp.set_defaults(func=cmd_fetch)

    sp = add_submit_args(add_base(sub.add_parser("run", help="submit→poll→results→fetch")))
    sp.add_argument("--file-path", nargs="*", help=argparse.SUPPRESS)
    sp.add_argument("--result-path", nargs="*", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("summary", help="받아온 원응답으로 계약 점검 (네트워크 불필요)")
    sp.set_defaults(func=cmd_summary)

    args = p.parse_args()

    base = Path(args.out)
    if args.run_dir:
        run = Run(Path(args.run_dir))
    elif args.cmd == "summary":
        run = Run(latest_run(base))
    elif args.new_run:
        base.mkdir(parents=True, exist_ok=True)
        run = Run(base / datetime.now().strftime("%Y%m%d-%H%M%S"))
    else:
        # **기본은 최근 run 을 이어 쓴다.** login 이 받은 토큰과 submit 이 받은
        # file_paths 가 같은 state.json 에 모여야 다음 단계가 인자 없이 이어진다.
        # 새로 시작하려면 --new-run 을 준다.
        base.mkdir(parents=True, exist_ok=True)
        runs = sorted(p for p in base.glob("*") if p.is_dir())
        run = Run(runs[-1] if runs else base / datetime.now().strftime("%Y%m%d-%H%M%S"))
    print(f"run dir: {run.dir}\n")
    args.func(args, run)


if __name__ == "__main__":
    main()
