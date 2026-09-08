"""선택된 광고 템플릿의 구분값을 영역 안 줄 범위에 연결한다."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..vlm import client as vlm_client
from .catalog import load_catalog, template_item_map


ABSTAIN = "해당없음"
MAX_REGIONS_PER_REQUEST = 10
MAX_LINES_PER_REQUEST = 90
MAX_CHARS_PER_REQUEST = 9000
_LEADING = re.compile(r"^[\s\-–—※•·▪◦■□*]+")
_SPACE_PUNCT = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)


def _norm(value: str) -> str:
    return _SPACE_PUNCT.sub("", value.casefold())


def _definitions(template: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in template["items"]:
        samples = []
        requirements = []
        for entry in item.get("entries") or []:
            example = " ".join(str(entry.get("example") or "").split())
            if example and example not in samples:
                samples.append(example[:280])
            required = str(entry.get("required") or "")
            instructions = " ".join(str(entry.get("instructions") or "").split())
            descriptor = f"필수여부={required}" + (f", {instructions}" if instructions else "")
            if descriptor not in requirements:
                requirements.append(descriptor[:240])
        sample_text = " / ".join(samples[:2])
        requirement_text = " / ".join(requirements[:2])
        rows.append(
            f"- {item['gubun']}: 예시={sample_text or '(없음)'}; {requirement_text}"
        )
    return "\n".join(rows)


def _region_text(region: dict[str, Any]) -> str:
    lines = [
        f"[{index:02d}] {str(line.get('text') or '').strip()}"
        for index, line in enumerate(region.get("lines") or [])
    ]
    bbox = region.get("bbox")
    return (
        f"REGION {region.get('region_id')} "
        f"layout={region.get('label')} score={region.get('layout_score')} "
        f"role_hint={region.get('role')} bbox={bbox}\n" + "\n".join(lines)
    )


def _batches(regions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    line_count = char_count = 0
    for region in regions:
        region_lines = len(region.get("lines") or [])
        region_chars = len(_region_text(region))
        if current and (
            len(current) >= MAX_REGIONS_PER_REQUEST
            or line_count + region_lines > MAX_LINES_PER_REQUEST
            or char_count + region_chars > MAX_CHARS_PER_REQUEST
        ):
            batches.append(current)
            current, line_count, char_count = [], 0, 0
        current.append(region)
        line_count += region_lines
        char_count += region_chars
    if current:
        batches.append(current)
    return batches


def _schema(region_ids: list[str], gubuns: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_ids},
                        "line_from": {"type": "integer"},
                        "line_to": {"type": "integer"},
                        "gubun": {"type": "string", "enum": [*gubuns, ABSTAIN]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "region_id", "line_from", "line_to", "gubun", "confidence", "reason",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


def _explicit_label(text: str, gubuns: set[str]) -> str | None:
    stripped = _LEADING.sub("", text).strip()
    normalized = _norm(stripped)
    aliases = {
        "금리": ("기본금리", "최고금리", "최저금리", "연이자율"),
        "대출금리": ("대출금리",),
        "심의번호": ("심의번호", "준법감시인심의필"),
        "회사명": ("nh농협은행", "nh농협카드", "농협은행", "농협카드"),
    }
    for gubun in sorted(gubuns, key=len, reverse=True):
        target = _norm(gubun)
        if normalized == target or normalized.startswith(target + "은"):
            return gubun
        # 표제 뒤에 값이 이어진 경우만 허용한다. '상품 안내'를 '상품명'으로 보는 식의
        # 부분 문자열 판정은 하지 않는다.
        if re.match(rf"^{re.escape(gubun)}\s*[:：|]", stripped):
            return gubun
    for gubun, values in aliases.items():
        if gubun in gubuns and any(normalized.startswith(_norm(value)) for value in values):
            return gubun
    return None


def _fixed_phrases(template: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for item in template["items"]:
        for entry in item.get("entries") or []:
            example = _norm(str(entry.get("example") or ""))
            # 숫자 자리표시자가 있는 예시는 정형 문구가 아니다. 긴 고정 고지문만
            # 결정론적으로 매칭하고 변수형 문장은 의미 판정에 맡긴다.
            if len(example) >= 12 and "0" not in example:
                values.append((item["gubun"], example))
    return values


def _state() -> dict[str, Any]:
    return {"sources": set(), "confidences": [], "reasons": []}


def _add(
    states: dict[str, dict[str, Any]], label: str, source: str,
    confidence: float | None = None, reason: str = "",
) -> None:
    value = states.setdefault(label, _state())
    value["sources"].add(source)
    if confidence is not None and 0.0 <= confidence <= 1.0:
        value["confidences"].append(confidence)
    if reason and reason not in value["reasons"]:
        value["reasons"].append(reason)


def _compress(states: list[dict[str, dict[str, Any]]], template_id: str) -> list[dict[str, Any]]:
    labels = sorted({label for line in states for label in line})
    output: list[dict[str, Any]] = []
    for label in labels:
        points: list[tuple[int, tuple[Any, ...]]] = []
        for index, line in enumerate(states):
            if label not in line:
                continue
            value = line[label]
            signature = (
                tuple(sorted(value["sources"])),
                round(max(value["confidences"]), 4) if value["confidences"] else None,
                tuple(value["reasons"]),
            )
            points.append((index, signature))
        spans: list[dict[str, Any]] = []
        if points:
            start = previous = points[0][0]
            signature = points[0][1]
            for index, current_signature in points[1:]:
                if index == previous + 1 and current_signature == signature:
                    previous = index
                    continue
                spans.append(_span(start, previous, signature))
                start = previous = index
                signature = current_signature
            spans.append(_span(start, previous, signature))
        output.append({
            "label_id": f"{template_id}:{label}",
            "label": label,
            "spans": spans,
        })
    return output


def _span(start: int, end: int, signature: tuple[Any, ...]) -> dict[str, Any]:
    sources, confidence, reasons = signature
    result: dict[str, Any] = {
        "line_from": start,
        "line_to": end,
        "sources": list(sources),
    }
    if confidence is not None:
        result["confidence"] = confidence
    if reasons:
        result["reasons"] = list(reasons)
    return result


def label_regions(
    doc: dict[str, Any],
    resolution: dict[str, Any],
    catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """선택 템플릿의 구분 enum으로 VLM 판정을 제한하고 규칙 근거를 병합한다."""
    template_id = resolution.get("template_id")
    if not template_id:
        return {
            "status": "skipped",
            "template_id": None,
            "by_region": {},
            "notes": ["템플릿이 확정되지 않아 구분값 판정을 건너뜀"],
        }
    active = catalog or load_catalog()
    template = active["templates"].get(template_id)
    if template is None:
        raise ValueError(f"카탈로그에 없는 템플릿: {template_id!r}")
    item_map = template_item_map(template_id, active)
    gubuns = list(item_map)
    gubun_set = set(gubuns)
    fixed_phrases = _fixed_phrases(template)
    region_index = {
        str(region.get("region_id")): region
        for page in doc.get("pages") or []
        for region in page.get("regions") or []
        if region.get("lines") and not region.get("is_illustrative")
    }
    states_by_region: dict[str, list[dict[str, dict[str, Any]]]] = {
        region_id: [defaultdict(_state) for _ in region.get("lines") or []]
        for region_id, region in region_index.items()
    }
    notes: list[str] = []

    for batch in _batches(list(region_index.values())):
        region_ids = [str(region["region_id"]) for region in batch]
        prompt = f"""당신은 농협 금융광고의 템플릿 구분값 분류기입니다.
템플릿은 {template_id}로 이미 결정되었습니다. 각 REGION의 줄을 읽고 해당되는 구분값과
정확한 줄 범위를 반환하세요. 한 영역에 여러 구분값이 있으면 verdict를 여러 개 만드세요.
광고 헤드라인·버튼·일반 홍보처럼 어느 구분에도 속하지 않으면 해당없음을 반환하거나
생략하세요. 예시문구와 표현이 달라도 의미가 같으면 같은 구분입니다. 제공된 enum 밖의
구분값은 만들지 마세요.

구분값과 원본 템플릿 예시:
{_definitions(template)}

판정 대상:
{chr(10).join(_region_text(region) for region in batch)}
"""
        try:
            response = vlm_client.chat_json(
                [{"type": "text", "text": prompt}],
                schema_name="ad_template_region_labels",
                schema=_schema(region_ids, gubuns),
                max_tokens=2800,
            )
        except Exception as exc:
            notes.append(f"구분값 VLM 판정 실패({','.join(region_ids)}): {exc}")
            continue
        for verdict in response.get("verdicts") or []:
            region_id = str(verdict.get("region_id") or "")
            label = str(verdict.get("gubun") or "")
            if region_id not in states_by_region or label not in gubun_set:
                continue
            line_states = states_by_region[region_id]
            if not line_states:
                continue
            start = int(verdict.get("line_from", 0))
            end = int(verdict.get("line_to", start))
            if end < start:
                start, end = end, start
            start, end = max(0, start), min(len(line_states) - 1, end)
            if start > end:
                continue
            confidence = float(verdict.get("confidence") or 0.0)
            reason = str(verdict.get("reason") or "")
            for index in range(start, end + 1):
                _add(line_states[index], label, "semantic_vlm", confidence, reason)

    # VLM 뒤에 결정론적 근거를 적용한다. 명시적인 표제 줄에서는 다른 VLM 라벨을
    # 제거해 "가입기간" 표제가 "가입금액"으로 덮이는 종류의 오류를 차단한다.
    for region_id, region in region_index.items():
        line_states = states_by_region[region_id]
        for index, line in enumerate(region.get("lines") or []):
            text = str(line.get("text") or "")
            explicit = _explicit_label(text, gubun_set)
            if explicit:
                for other in list(line_states[index]):
                    if other != explicit and line_states[index][other]["sources"] == {"semantic_vlm"}:
                        del line_states[index][other]
                _add(line_states[index], explicit, "explicit_heading", 1.0, "줄의 명시 표제")
            normalized = _norm(text)
            for label, phrase in fixed_phrases:
                if phrase in normalized:
                    _add(line_states[index], label, "fixed_phrase", 1.0, "템플릿 고정문구 일치")

    by_region = {
        region_id: _compress(states, template_id)
        for region_id, states in states_by_region.items()
    }
    return {
        "status": "partial" if notes else "complete",
        "template_id": template_id,
        "by_region": by_region,
        "notes": notes,
    }
