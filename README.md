# nh-ad-parser

광고물(PDF·이미지·HWP)을 넣으면 **글자·좌표·영역 원본**, **템플릿·판독 근거(P1)**,
**다음 심의 단계용 영역 입력(P3)** JSON이 나온다.

```
입력 ─► 판별·OCR·영역 조립 ─► parse ─► 템플릿 선택·구분 라벨 ─► evidence(P1)
                                                        └─► review-input(P3)
```

`nh-ad-review-poc` 에서 **파싱 계층만** 떼어내 구조를 다시 잡은 저장소다.
실험 산출물·일회성 도구·미확정 코드는 따라오지 않았다. 이관 근거와 경계는
[docs/이관-기록.md](docs/이관-기록.md).

## 빠른 시작

```bash
uv sync
cp .env.example .env      # PADDLEX_URL / GEMMA_URL / GEMMA_MODEL 채우기
uv run python tools/parse.py --input <파일 또는 폴더> --out out/<날짜>/<라벨> --preview
```

`out/` 을 날짜별로 정리하는 규칙과 예시는
[docs/테스트-실행-지침.md](docs/테스트-실행-지침.md) 참고.

```
▶ [예금성상품-적금] 올원e적금.png
  쪽 1 / 영역 38 / 줄 73 / ...초  → out\<날짜>\<라벨>\parse\...json
  템플릿 예금성상품-적립식 (confirmed) / 라벨 ... / 검수영역 ...
  P1 → out\<날짜>\<라벨>\evidence\...json
  P3 → out\<날짜>\<라벨>\review-input\...json
```

기본 실행은 영역별 VLM Reader/Judge까지 수행한다. OCR·레이아웃 원본만 빠르게 확인하려면
`--parse-only`를 사용한다. 전체 출력에서 Reader/Judge만 끄려면 `--region-reading off`를
명시한다.

개발용 PaddleX·Gemma 엔드포인트는 `.env`에만 설정한다. `GEMMA_MODEL`이 실제
게이트웨이의 `/models` 응답과 다르면 VLM 단계만 실패하고 OCR 산출물은 생성될 수 있으므로
실행 전에 두 값을 함께 확인한다.

> **진행 중:** 농협 내부 플랫폼(AgileSoDA `ETL with LLM`)으로 OCR 계층을 옮기는
> 작업이 있다. 이 API 는 **농협 폐쇄망 안에서만 호출**할 수 있어 현장 방문이 유일한
> 검증 창구다.
>
> | 문서·도구 | 역할 |
> | --- | --- |
> | [docs/농협-ocr-연동-규격.md](docs/농협-ocr-연동-규격.md) | 받은 API 가이드 58쪽 정리 (모순·공백 포함) |
> | [docs/농협-ocr-전환-계획.md](docs/농협-ocr-전환-계획.md) | 격차 11건 · 전환 설계 · 회신 질문 |
> | [docs/농협-ocr-현장점검.md](docs/농협-ocr-현장점검.md) | 현장 절차·반입물·시험 항목 T1~T11 |
> | `ocr/etlwithllm.py` | Default JSON → IR 변환기 |
> | [integrations/etlwithllm-review/README.md](integrations/etlwithllm-review/README.md) | 외부 API / transform / CUSTOMIZE 후보와 KL Custom Parser 비교 |
> | `tools/etl_probe.py` | 현장 반입용 프로브. **의존성 0, 단일 파일** |
> | `tools/etl_mock.py` | 방문 전 리허설용 목 서버 |
> | [docs/kl-parser-선택적-api.md](docs/kl-parser-선택적-api.md) | 기존 AWX API를 현재 광고 파이프라인 앞단에 선택적으로 붙이는 방법 |
> | [docs/A안-KL-전체흐름-폐쇄망실행.md](docs/A안-KL-전체흐름-폐쇄망실행.md) | A안의 실제 코드 흐름, KL 호출 의미, 폐쇄망 실행 조건과 미확정 사항 |
> | [docs/농협-A안-Jupyter-실행-전달.md](docs/농협-A안-Jupyter-실행-전달.md) | Jupyter 실행 순서, 환경 점검, 전달물과 현장 확인값 |
>
> `ocr/paddlex.py` 는 A/B 기준선으로 남긴다.

Knowledge Lake/AWX의 비동기 광고 API가 필요하다고 확정된 경우에만 선택적 서버를
실행한다. 일반 광고 파싱에는 필요하지 않다.

```bash
uv sync --extra kl-api
uv run --extra kl-api python tools/run_kl_parser.py --host 127.0.0.1 --port 9101
```

## 산출물 구조

산출물은 역할이 다른 세 층으로 나뉜다.

| 폴더 | 역할 |
| --- | --- |
| `parse/` | 파서의 원시 `AdDocument`. 기존 호환 및 파싱 결함 진단용 |
| `evidence/` | P1. parse 전부 + `line_ref` + VLM/Judge + 템플릿 선택 + 구분값 span |
| `review-input/` | P3. 영역을 유지하고 OCR/VLM 중 선택된 문구 하나를 다음 심의 단계에 전달 |

세부 계약과 텍스트 선택 정책은 [docs/출력-계약.md](docs/출력-계약.md) 참고.

```jsonc
{
  "doc_id": "...", "source_file": "...", "file_type": "image",
  "product_group": "예금성", "ad_type": "상세페이지",   // 파일명·내용 기반 분류
  "pages": [{
    "page_no": 1, "canvas_w": 1122, "canvas_h": 6429, "dpi": 200,
    "parse_route": "ocr",          // ocr | digital | hybrid
    "parse_status": "ok",
    "regions": [{
      "region_id": "p1_r002",
      "bbox": [65, 349, 220, 402],
      "label": "text",             // PP-DocLayout_plus-L 의 20클래스
      "layout_score": 0.98,
      "role": "본문",              // 규칙 기반 — 아래 "범위" 참고
      "card_no": null,             // 카드형 광고에서 몇 번째 카드인지
      "table": null,               // 표면 행·열·셀 좌표
      "lines": [{
        "text": "연이자율",
        "bbox": [65, 349, 220, 402],
        "confidence": 0.998,
        "source": "ocr",           // ocr | digital | hwp
        "style": null              // pt·색·굵기 (디지털/HWP 입력일 때)
      }]
    }],
    "unassigned_lines": []         // 어느 영역에도 못 붙은 줄. 0 이어야 정상
  }]
}
```

**모든 글자는 어딘가에 남는다.** 영역에 못 붙은 줄도 `unassigned_lines` 로 싣는다.
줄이 사라지면 그 자체가 결함이다.

## 폴더 구조

| 경로 | 하는 일 |
| --- | --- |
| `src/nh_parser/config.py` | 모든 설정 한 곳. 임계치마다 왜 그 값인지 실측 근거가 주석에 있다 |
| `src/nh_parser/ir.py` | 데이터 모델 (`AdDocument` / `AdPage` / `Region` / `Line`) |
| `src/nh_parser/ingest/` | 입력 적재 — `triage`(형식·텍스트레이어 판별) `canvas`(렌더) `hwp` `assets` `text_style` |
| `src/nh_parser/ocr/` | `paddlex`(PP-StructureV3 호출) `tiling`(타일 분할) `bands`(글자밀도 밴드) |
| `src/nh_parser/layout/` | `regions`(줄→영역 배정) `cards`(카드 분할) `gap`(수직 갭 분리) |
| `src/nh_parser/vlm/` | `client`(Gemma) `cache` `direct` `view` `field_judge` `reading`(판독 교차검증) |
| `src/nh_parser/review/` | 19종 템플릿 선택, 영역별 구분값 판정, P1/P3 출력 계약 |
| `src/nh_parser/templates/ad_templates.json` | 농협 HWPX 표에서 생성한 19종 템플릿 카탈로그 |
| `src/nh_parser/pipeline.py` | 위를 순서대로 엮는다. 진입점은 `process_file(path)` |
| `tools/parse.py` | parse/P1/P3를 한 번에 만드는 CLI |
| `tools/build_review.py` | 저장된 parse JSON에서 OCR 재실행 없이 템플릿·P1·P3 재생성 |
| `tools/build_template_catalog.py` | 템플릿 HWPX의 실제 표 셀에서 카탈로그 재생성 |
| `tests/` | 224 통과 / 37 건너뜀(샘플 PDF 필요, 2026-09-09 기준) |

## 범위 — 여기 없는 것

이 저장소는 **"무엇이 어디에 적혀 있고, 확정 템플릿의 어느 구분값인가"까지**다.
"그래서 규정에 맞나"는 다음 심의 단계의 책임이다.

| 없는 것 | 어디 있(었)나 |
| --- | --- |
| 규정 검색·준수 여부 판정 | 다음 심의 엔진 |
| 웹 검수 화면 | 아직 이관하지 않음 |
| 필드 추출·정규화 내보내기 | `extract.py`, `normalized_export.py`, `kl_export.py` |
| VLM 역할 판정 | `vlm_judge.py` |

**`region.role` 은 규칙 기반 폴백값이다.** 원래 설계는 VLM(`vlm_judge`)이 이걸
덮어쓰는 것이고, 그 모듈은 라벨링 계층에 있어 여기 없다. 규칙 결과를 최종 역할로
믿으면 안 된다.

템플릿 구분값 판정은 인공적으로 작성한 라벨 설명 대신 농협 HWPX의 실제 예시문구와
기재요령을 사용한다. VLM 출력은 선택된 템플릿의 구분 enum 밖으로 나갈 수 없다.

## HWP 입력

HWP/HWPX 는 사내 `document-processor` 가 있어야 한다. 사설 저장소라 `pyproject.toml`
에 못 박지 않았다 — 넣으면 접근 권한 없는 곳에서 `uv sync` 자체가 실패한다.
`ingest/hwp.py` 가 함수 안에서 지연 import 하므로 없으면 **HWP 경로만** 막히고
PDF·이미지는 정상 동작한다.

```bash
uv pip install "document-processor @ git+ssh://git@github.com/CGINSIDE-ROOKIES/document-processor.git"
```

## 알려진 한계

- **한국어 OCR 정확도 천장 88%.** 서버가 `korean_PP-OCRv5_mobile_rec` 를 쓰는데
  PP-OCRv5 에 한국어 server 인식 모델이 아예 없다. 파라미터로 못 넘는다.
- **특수문자가 깨진다.** 실측: `① → 1`, `「」 → []`, `⑦번동의서 → 번동의서`,
  `동의 시¹ → 동의 시1`. 마지막 것은 각주 참조와 값을 구별 못 하게 만든다.
- **한 덩어리가 두 영역으로 갈리는 경우가 남아 있다.** 실측(올원e적금 36블록 중 3건):
  숫자와 그 조건, 표제와 본문, 목록 ①②와 ③이 갈렸다.
- **VLM 호출은 같은 입력에도 흔들린다.** 서버가 fp8 KV 캐시·prefix caching·chunked
  prefill·투기 디코딩을 켠 채로 떠 있어 `temperature=0` 으로도 못 막는다.
  카드 분할·밴드 판독처럼 VLM 을 쓰는 단계는 재실행하면 결과가 달라질 수 있다.
- 표로 검출되지 않는 2열 항목표(`대출대상 | 내용`)는 짝을 못 맞춘다.
