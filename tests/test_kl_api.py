from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest


pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from nh_parser.ir import AdDocument
from nh_parser.kl_api.app import _decode_option, _safe_filename, create_app
from nh_parser.kl_api import service
from nh_parser.kl_api.service import KlBackendConfigError, configured_backend
from nh_parser.kl_api.status import DONE, ERROR, write_status


def _successful_worker(work_dir, image_dir, file_path, option):
    work = Path(work_dir)
    source = Path(file_path)
    (work / f"{source.name}_parsed.json").write_text(
        json.dumps({"source": source.name, "option": option}, ensure_ascii=False),
        encoding="utf-8",
    )
    (work / f"{source.name}_review_input.json").write_text("{}", encoding="utf-8")
    (work / "ad_summary.json").write_text("{}", encoding="utf-8")
    images = Path(image_dir)
    images.mkdir(parents=True, exist_ok=True)
    (images / "광고_p1.jpg").write_bytes(b"jpeg-placeholder")
    write_status(work, DONE)


def _failed_worker(work_dir, image_dir, file_path, option):
    write_status(Path(work_dir), ERROR, "synthetic failure")


def test_ad_roundtrip_returns_awx_zip_and_cleans_job(tmp_path: Path):
    app = create_app(
        worker=_successful_worker,
        work_root=tmp_path,
        keep_work_dir=False,
        timeout_s=77,
    )
    with TestClient(app) as client:
        option = base64.b64encode(
            json.dumps({"request_id": "R-1"}).encode("utf-8")
        ).decode("ascii")
        accepted = client.post(
            "/ad/parsing",
            files={"src_file": ("광고.png", b"image", "image/png")},
            data={"option": option},
        )
        assert accepted.status_code == 202
        body = accepted.json()["body"]
        assert body["timeout"] == 77
        job_id = body["uuid"]

        result = client.get(f"/parsing/result/{job_id}")
        assert result.status_code == 200
        assert result.headers["content-type"].startswith("application/x-zip-compressed")
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            assert set(archive.namelist()) == {
                "광고.png_parsed.json",
                "광고.png_review_input.json",
                "ad_summary.json",
                "광고.png_img.zip",
            }
            parsed = json.loads(archive.read("광고.png_parsed.json"))
            assert parsed["option"] == {"request_id": "R-1"}
        assert not (tmp_path / job_id).exists()


def test_error_status_is_returned_without_zip(tmp_path: Path):
    app = create_app(
        worker=_failed_worker, work_root=tmp_path, keep_work_dir=True,
    )
    with TestClient(app) as client:
        accepted = client.post(
            "/ad/parsing",
            files={"src_file": ("ad.pdf", b"pdf", "application/pdf")},
        )
        job_id = accepted.json()["body"]["uuid"]
        result = client.get(f"/parsing/result/{job_id}")
    assert result.json() == {"status": "ERROR", "message": "synthetic failure"}


def test_general_rag_endpoint_is_explicitly_not_implemented(tmp_path: Path):
    app = create_app(
        worker=_successful_worker, work_root=tmp_path, keep_work_dir=True,
    )
    with TestClient(app) as client:
        response = client.post("/parsing")
    assert response.status_code == 501
    assert "광고 파싱 전용" in response.json()["message"]


def test_invalid_or_missing_uuid_is_not_a_filesystem_path(tmp_path: Path):
    app = create_app(
        worker=_successful_worker, work_root=tmp_path, keep_work_dir=True,
    )
    with TestClient(app) as client:
        response = client.get("/parsing/result/not-a-uuid")
    # 전달받은 AWX 예제가 정의한 미존재 UUID 응답 코드를 그대로 유지한다.
    assert response.status_code == 500
    assert response.json()["message"] == "Requested url does not exist."


def test_option_decoder_accepts_only_base64_json_object():
    encoded = base64.b64encode(json.dumps({"a": 1}).encode()).decode()
    assert _decode_option(encoded) == {"a": 1}
    assert _decode_option(base64.b64encode(b"[]").decode()) is None
    assert _decode_option("not-base64") is None


def test_uploaded_filename_cannot_escape_job_directory():
    assert _safe_filename("../광고.png") == "광고.png"
    assert _safe_filename("..") == "upload.bin"


def test_backend_name_is_fail_closed(monkeypatch):
    monkeypatch.setenv("NH_KL_AD_BACKEND", "unknown")
    with pytest.raises(KlBackendConfigError, match="허용값"):
        configured_backend()


def test_current_p1_p3_are_exported_with_reported_kl_names(
    tmp_path: Path, monkeypatch,
):
    source = tmp_path / "광고.png"
    source.write_bytes(b"image")
    document = AdDocument(doc_id="광고", source_file=source.name, file_type="image")
    monkeypatch.setattr(
        service,
        "resolve_template",
        lambda parsed: {"template_id": None, "status": "unresolved"},
    )
    monkeypatch.setattr(
        service,
        "label_regions",
        lambda parsed, resolution: {"template_id": None, "status": "skipped"},
    )
    monkeypatch.setattr(
        service,
        "build_evidence",
        lambda parsed, resolution, labeling: {
            **parsed,
            "classification": {"product_group": None},
            "template": resolution,
            "summary": {"region_count": 0, "line_count": 0},
        },
    )
    monkeypatch.setattr(
        service,
        "build_review_input",
        lambda evidence: {"summary": {"region_count": 0}},
    )

    service._write_ad_outputs(
        tmp_path, source, document, backend="etl", option={"request_id": "R-2"},
    )

    assert (tmp_path / "광고.png_parsed.json").is_file()
    assert (tmp_path / "광고.png_review_input.json").is_file()
    summary = json.loads((tmp_path / "ad_summary.json").read_text(encoding="utf-8"))
    assert summary["parser_backend"] == "etl"
    assert summary["request_option"] == {"request_id": "R-2"}
