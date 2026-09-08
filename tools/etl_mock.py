# -*- coding: utf-8 -*-
"""`etl_probe.py` 리허설용 목 서버 — **현장 가기 전에 스크립트를 검증한다.**

농협 폐쇄망 방문은 사실상 1회성이다. 거기서 프로브 스크립트의 버그를 발견하면
되돌릴 방법이 없다. 그래서 가이드의 응답 예시를 그대로 흉내내는 서버를 띄워
전 구간을 미리 돌려 본다. 실제로 이 리허설에서 결함 1건을 잡았다(단계별 실행 시
run 디렉터리가 갈려 login 토큰이 유실되던 문제).

**이 목 서버는 규격 문서를 근거로 한 추측이다.** 실물 서버가 다르게 답할 수 있다.
리허설의 목적은 "우리 스크립트가 죽지 않는가" 이고, "규격이 맞는가" 가 아니다.

사용법 — 검증할 명령을 인자로 그대로 넘긴다:

    python tools/etl_mock.py python tools/etl_probe.py run \
        --base-url http://127.0.0.1:58999 --ws-id WS0001 \
        --author me --input sample.pdf

목 서버가 127.0.0.1:58999 에 떴다가 명령이 끝나면 함께 내려간다.
"""
import json, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

FP = "test1.pdf/v1/test1.pdf"
RP = "test1.pdf/v1/test1.json"
POLLS = {"n": 0}

DOC = {
  "pdfName": "test1.pdf", "pageLen": 1,
  "pages": [{
    "pageId": 1, "width": 1275, "height": 1650,
    "paragraphs": [
      {"paragraphId": 0, "type": "Text",
       "bbox": [[184,206],[568,206],[568,232],[184,232]],
       "contents": "This is a sample test document.", "confidence": 0.998,
       "lines": [{"bbox": [[187,206],[567,206],[567,235],[187,235]],
                  "contents": "This is a sample test document."}]},
      {"paragraphId": 1, "type": "Title",
       "bbox": [[183,267],[445,267],[445,291],[183,291]],
       "contents": "Section 1: Introduction", "confidence": 0.852,
       "lines": [{"bbox": [[187,267],[444,267],[444,294],[187,294]],
                  "contents": "Section 1: Introduction"}]},
      {"paragraphId": 10, "type": "Table",
       "bbox": [[149,776],[1126,776],[1126,909],[149,909]],
       "contents": "<table><tr><td>나는 표 테이터</td></tr></table>",
       "confidence": 0.999, "lines": [], "rows": 3, "cols": 3,
       "cells": [
         {"bbox": [[153,776],[474,776],[474,822],[153,822]],
          "contents": "나는 표 테이터", "cellId": "C0",
          "confidence": -1, "cell_info": [0,0,0,0]},
         {"bbox": [[153,822],[474,822],[474,909],[153,909]],
          "contents": "일반", "cellId": "C1",
          "confidence": -1, "cell_info": [1,2,0,0]}]}],
    "warning_messages": [{"code": "USE_OCR_PAGE", "detail": {}}], "n_cols": 1}]}


def ok(data):
    return {"result": {"code": 0, "message": "success"}, "meta": None, "data": data}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path.endswith("/user/login"):
            return self._send(ok({"email": "u@x", "name": "u", "role": "004"}),
                              cookie="token=abc123xyz; Path=/; HttpOnly")
        if self.path.endswith("/workspace/list"):
            return self._send(ok([{"id": "WS0001", "workspace_name": "Test",
                                   "access_level": "002", "source_cnt": 3}]))
        if self.path.endswith("/etl/auto/start"):
            ct = self.headers.get("Content-Type", "")
            assert ct.startswith("multipart/form-data; boundary="), ct
            assert b'name="tr_data"' in raw and b'name="upfiles"' in raw
            tr = raw.split(b'name="tr_data"')[1].split(b"\r\n\r\n")[1].split(b"\r\n--")[0]
            print("  [mock] tr_data =", tr.decode("utf-8"), file=sys.stderr)
            print("  [mock] upfiles =", raw.count(b'name="upfiles"'), file=sys.stderr)
            return self._send(ok({"task_ids": ["39d39765-1711-46e1-858d-417f0f488ef8"],
                                  "file_paths": [FP], "meta_info": {}, "msg": "add tasks"}))
        self._send({"detail": "Method Not Allowed"})

    def do_GET(self):
        if self.path.startswith("/api/v1/file/info"):
            POLLS["n"] += 1
            cs = "002" if POLLS["n"] >= 2 else "001"
            return self._send(ok({"datasource": [
                {"id": "x", "file_id": "y", "version_number": 1, "file_path": FP,
                 "chunk_status": cs, "chunk_step": "002", "is_active": True,
                 "created_by": "anonymous"}]}))
        if self.path.startswith("/api/v1/file/result/list"):
            return self._send(ok([
                {"file_name": "pipeline_log.log", "file_path": "test1.pdf/v1/pipeline_log.log"},
                {"file_name": "test1.json", "file_path": RP},
                {"file_name": "test1.pdf", "file_path": FP},
                {"file_name": "test1_pages.json", "file_path": "test1.pdf/v1/test1_pages.json"}]))
        if self.path.startswith("/api/v1/file/result/doc"):
            return self._send(DOC)
        self._send({"detail": [{"type": "missing", "loc": ["query", "wsId"],
                                "msg": "Field required", "input": None}]})


srv = HTTPServer(("127.0.0.1", 58999), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
rc = subprocess.call(sys.argv[1:])
srv.shutdown()
sys.exit(rc)
