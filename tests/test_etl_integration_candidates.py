import json
from pathlib import Path

import pytest
from PIL import Image

from nh_parser.integrations.etlwithllm.client import EtlApiError, EtlWithLlmClient
from nh_parser.integrations.etlwithllm import pipeline as etl_pipeline
from nh_parser.ocr.etlwithllm import (
    EtlDefaultJsonError,
    load_default_document,
    normalize_default_document,
)


def _default_doc(name: str = "sample.png") -> dict:
    return {
        "pdfName": name,
        "pageLen": 1,
        "pages": [{
            "pageId": 1,
            "width": 50,
            "height": 100,
            "paragraphs": [{
                "paragraphId": 1,
                "type": "Text",
                "bbox": [[5, 10], [40, 10], [40, 30], [5, 30]],
                "contents": "농협 테스트",
                "confidence": 0.9,
                "lines": [{
                    "bbox": [[5, 10], [40, 10], [40, 30], [5, 30]],
                    "contents": "농협 테스트",
                }],
            }],
        }],
    }


@pytest.mark.parametrize(
    "payload",
    [
        _default_doc(),
        {"data": _default_doc()},
        {"doc_result": _default_doc()},
        {"doc_result": {"default": _default_doc()}},
    ],
)
def test_normalize_default_document_accepts_documented_variants(payload):
    assert normalize_default_document(payload)["pdfName"] == "sample.png"


def test_normalize_default_document_rejects_unrelated_pages():
    with pytest.raises(EtlDefaultJsonError):
        normalize_default_document({"pages": []})


def test_load_default_document_selects_matching_pdf_name(tmp_path: Path):
    (tmp_path / "a.json").write_text(json.dumps(_default_doc("a.pdf")), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(_default_doc("b.pdf")), encoding="utf-8")
    assert load_default_document(tmp_path, source_name="b.pdf")["pdfName"] == "b.pdf"


def test_external_api_rejects_undocumented_customize():
    with pytest.raises(ValueError, match="확인되지 않은"):
        EtlWithLlmClient._validate_project_config({"extractType": "customize"})
    with pytest.raises(ValueError, match="extractType"):
        EtlWithLlmClient._validate_project_config({"extract_type": "dla"})


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({
            "result": {"code": 0, "message": "success"},
            "data": {"task_ids": ["task-1"], "file_paths": ["sample.pdf/v1/sample.pdf"]},
        })

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url.endswith("/file/info"):
            return _Response({
                "result": {"code": 0, "message": "success"},
                "data": {"datasource": [{"chunk_status": "002", "chunk_step": "002"}]},
            })
        if url.endswith("/file/result/list"):
            return _Response({
                "result": {"code": 0, "message": "success"},
                "data": [
                    {"file_name": "sample_pages.json", "file_path": "x/sample_pages.json"},
                    {"file_name": "sample.json", "file_path": "x/sample.json"},
                ],
            })
        if url.endswith("/file/result/doc"):
            return _Response({"doc_result": {"default": _default_doc("sample.pdf")}})
        raise AssertionError(url)


def test_external_api_full_document_flow_uses_public_contract(tmp_path: Path):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"not parsed by fake server")
    session = _Session()
    client = EtlWithLlmClient(
        "http://etl.local", "WS1", "tester", session=session, request_timeout_s=3,
    )
    doc = client.analyze_file(source, project_config={"extractType": "dla"})
    assert doc["pdfName"] == "sample.pdf"
    sent = json.loads(session.posts[0][1]["data"]["tr_data"])
    assert sent["prj_config"] == {"extractType": "dla"}
    assert sent["res_type"] == "default"
    assert [call[1]["params"] for call in session.gets] == [
        {"file_path": "sample.pdf/v1/sample.pdf"},
        {"docPath": "sample.pdf/v1/sample.pdf"},
        {"docResultPath": "x/sample.json"},
    ]


def test_external_api_raises_platform_error():
    response = _Response({"result": {"code": -1, "message": "missing"}, "data": {}})
    with pytest.raises(EtlApiError, match="code=-1"):
        EtlWithLlmClient._response_data(response, "시험")


def test_process_file_from_dla_bypasses_paddlex_and_rescales(monkeypatch, tmp_path: Path):
    source = tmp_path / "sample.png"
    Image.new("RGB", (100, 200), "white").save(source)
    monkeypatch.setattr(etl_pipeline, "_apply_vlm_judgments", lambda page, image: None)
    monkeypatch.setattr(etl_pipeline, "_classify_into", lambda doc, image: None)

    result = etl_pipeline.process_file_from_dla(source, _default_doc())

    assert result.source_file == "sample.png"
    assert result.pages[0].canvas_w == 100
    assert result.pages[0].canvas_h == 200
    assert result.pages[0].regions[0].bbox == [10, 20, 80, 60]
    assert result.pages[0].regions[0].lines[0].text == "농협 테스트"
    assert any("ETLwithLLM" in note for note in result.pages[0].notes)
