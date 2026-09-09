# 인계 프롬프트 — 농협 OCR 연동 작업

이 파일 하나만 읽으면 상황 파악이 되도록 썼다. 원본 PDF 를 다시 읽을 필요는 없다.

---

## 1. 한 줄 상황

우리 광고 심의 파서가 지금은 사내 PaddleOCR 을 부르는데, 이걸 **농협 내부 플랫폼의
OCR(AgileSoDA `ETL with LLM`)** 로 바꿔야 한다. 그 API 가이드(58쪽)를 받아서 분석을
마쳤고, 이제 연동 코드를 만들 차례다.

## 2. 배경

- 우리 프로젝트: 농협 광고물(적금·대출·카드 광고 등)을 넣으면 글자·좌표·영역을
  뽑아 다음 "심의" 단계로 넘기는 파서다. 저장소는 `nh-ad-parser`.
- 지금 OCR 은 사내 DGX-Spark 서버의 PaddleX(PP-StructureV3)를 HTTP 로 부른다.
  VLM(Gemma)도 같이 부른다.
- 농협은 자기네 내부(폐쇄망) 플랫폼 OCR 을 쓰라고 한다. 그 플랫폼이 AgileSoDA 의
  `ETL with LLM` 이다.
- **이 API 는 농협 폐쇄망 안에서만 호출된다.** 그래서 실제 호출 테스트는 농협에
  방문해서만 할 수 있고, 사실상 1회성이다.
- 받은 PDF 는 텍스트 레이어가 없는 이미지 PDF 였다(`pdftotext` 결과 58바이트).
  페이지 이미지를 판독해서 [농협-ocr-연동-규격.md](농협-ocr-연동-규격.md) 에 정리했다.

## 3. 중요 — 이 API 는 "OCR API" 가 아니다

단일 요청으로 "이미지 주면 글자 준다" 가 아니다. **파일을 올려서 분석 작업을 만들고,
상태를 조회해서, 결과 JSON 을 받아오는 비동기 플랫폼**이다.

```text
파일 업로드 + 분석요청 → 상태 폴링 → 결과 파일 목록 조회 → 결과 내용 조회
```

그리고 돌려주는 것은 OCR 텍스트만이 아니라 **레이아웃 분석 결과**다 — 영역 종류,
4점 좌표, 텍스트, 표 HTML. 즉 우리 PaddleX PP-StructureV3 자리를 통째로 대체한다.

### 약어 (가이드가 구분해 쓴다)

| 약어 | 뜻 | 하는 일 | 근거 |
|---|---|---|---|
| **DLA** | Document Layout Analysis | 영역 검출 + 종류 분류(9종) | p.1 정의, p.30 `dla_score_th` |
| **STR** | Scene Text Recognition | **글자 인식** (좁은 의미의 OCR) | p.30 `str_score_th` |
| **TSR** | Table Structure Recognition | 표 행·열 구조 | p.29 `tsr_model_name` |
| **CD** | Cell Detection | 표 셀 검출 | p.56 "CD 모델 confidence (현재는 -1)" |

**중요:** 결과 JSON 의 `confidence` 는 **DLA(영역 검출) 신뢰도뿐**이다. STR(글자 인식)
신뢰도는 내부에서 `str_score_th` 로 걸러내는 데만 쓰고 결과에 담아 주지 않는다.
§9 의 10번 항목이 이 이야기다.

대응되는 짝이 하나가 아니라 셋이므로, 이 전환은 "OCR 엔진 교체" 가 아니라
**PP-StructureV3 계층 전체 교체**다.

```text
PaddleX PP-StructureV3 =  레이아웃 검출  +  PP-OCR(글자 인식)  +  표 인식
ETL with LLM           =  DLA           +  STR               +  TSR / CD
```

## 4. 우리 파이프라인이 어떻게 바뀌는가

```text
[지금]
파일 → 텍스트레이어 판정(triage) → 200dpi 렌더 → 타일 분할
     → PaddleX 호출 → 좌표 복원 · 중복 제거
     → 영역 조립 → 템플릿 선택 · 구분 라벨 → P1/P3 출력 → 다음 심의 단계

[바뀐 뒤]
파일 → ETL 업로드 → 폴링 → 결과 JSON        ← 렌더 · 페이지분리 · OCR 을 플랫폼이 내부에서 한다
     → 연결 어댑터 + 변환기 (JSON → 우리 데이터 구조)
     → 영역 조립 → 템플릿 선택 · 구분 라벨 → P1/P3 출력 → 다음 심의 단계   ← 손대지 않는다
```

**전처리 4단계는 들어갈 자리가 없다.** 가이드 근거:

| 우리 전처리 | 플랫폼이 대신한다는 근거 (가이드 쪽수) |
|---|---|
| 200dpi 렌더 | p.55 `width`/`height` 설명이 "**이미지 변환 후** 너비/높이" |
| 페이지 분리 | p.30 `start_page`/`end_page` 옵션 (파일 통째로 넣고 페이지 범위 지정) |
| 텍스트레이어 판정 | p.29 `ocr_only` = "ocr만 사용 또는 **ocr + pdf reader 병합** 결과 사용" |
| 타일 분할 | 페이지 단위로 결과가 온다 |

**주의:** 전처리를 영구히 버린다는 뜻은 아니다. 타일 분할과 `textDetLimitSideLen=2500` 은
PaddleX 가 세로로 긴 캔버스를 축소해서 작은 글씨(유의사항·심의필 문구)를 깨뜨린
실측 대응이었다. ETL 이 같은 약점을 갖는지는 **아직 모른다.** 현장에서 세로로 긴 광고
1건을 넣어 확인한 뒤 판단한다.

## 5. 우리가 실제로 쓸 API — 4개뿐

가이드에 API 가 23개 있는데 1.1~1.9 는 계정·팀 관리, 1.10~1.15 는 워크스페이스 관리다.
**둘 다 우리 일이 아니다** — 워크스페이스(`ws_id`)는 AgileSoDA 가 만들어서 전달해준다고
2026-09-08 회신으로 확인됐다.

호스트·포트는 아직 못 받았다. 가이드 에러 캡처에 개발 서버가 `192.168.100.170:58000` 으로
노출돼 있어 포트는 58000 으로 추정된다.

### (1) 분석 요청 — `POST /api/v1/etl/auto/start`

`multipart/form-data` 로 보낸다. 필드는 딱 두 개다.

- `tr_data` : JSON 을 **문자열로** 인코딩한 값
- `upfiles` : 파일. 같은 이름으로 여러 번 첨부 가능

`tr_data` 안의 필드:

| 항목 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `author` | str | 필수 | - | 사용자 이름 |
| `ws_id` | str | 필수 | - | 워크스페이스 ID (받아서 쓴다) |
| `callback_url` | str | 선택 | NULL | 완료 시 콜백 받을 URL |
| `res_type` | str 또는 list | 선택 | `default` | 결과 종류 |
| `prj_config` | dict | 선택 | 기본값 | 분석 옵션 (아래) |
| `meta_info` | dict | 선택 | NULL | 아무 값이나. 콜백 결과에 그대로 실려 돌아온다 |

응답 (가이드 p.31 실제 캡처):

```json
{
  "result": { "code": 0, "message": "success" },
  "meta": null,
  "data": {
    "task_ids": ["39d39765-1711-46e1-858d-417f0f488ef8"],
    "file_paths": ["test1.pdf/v1/test1.pdf"],
    "meta_info": {},
    "msg": "add tasks"
  }
}
```

`task_ids` 와 `file_paths` 는 업로드 순서대로 짝이 맞는 배열이다.
**이후 조회는 전부 `file_path` 로 한다.** `task_id` 는 분석 중지에만 쓴다.
`file_path` 형식은 `[파일명]/[버전]/[파일명]`.

### (2) 상태 확인 — `GET /api/v1/file/info?file_path={경로}`

응답 (p.33 실제 캡처):

```json
{
  "result": { "code": 0, "message": "success" },
  "meta": null,
  "data": { "datasource": [ {
    "id": "52a17a95-4f63-4b85-bfb1-e589ed3128a1",
    "file_id": "c176494b-b2f5-41cc-8079-1d33c05d1f6d",
    "version_number": 1,
    "file_path": "test1.pdf/v1/test1.pdf",
    "chunk_status": "002",
    "chunk_step": "002",
    "is_active": true,
    "created_by": "anonymous",
    "updated_by": "anonymous",
    "created_at": "2024-11-27T15:08:19",
    "updated_at": "2024-11-27T15:08:41"
  } ] }
}
```

`chunk_status` : `000` 초기 / `001` 진행중 / `002` 완료 / `999` 오류
`chunk_step` : `000` 준비 / `001` 파일 추출 / `002` 파일 변환

`002` 가 될 때까지 폴링한다. 권장 폴링 주기는 가이드에 없다.

### (3) 결과 파일 목록 — `GET /api/v1/file/result/list?docPath={경로}`

응답 (p.36 실제 캡처):

```json
{ "result": { "code": 0, "message": "success" }, "meta": null,
  "data": [
    { "file_name": "pipeline_log.log", "file_path": "test1.pdf/v1/pipeline_log.log" },
    { "file_name": "test1.json",       "file_path": "test1.pdf/v1/test1.json" },
    { "file_name": "test1.pdf",        "file_path": "test1.pdf/v1/test1.pdf" },
    { "file_name": "test1_edit.json",  "file_path": "test1.pdf/v1/test1_edit.json" }
  ] }
```

우리가 쓸 것은 `[파일명].json` (밑줄 접미사가 없는 것).

### (4) 결과 내용 — `GET /api/v1/file/result/doc?docResultPath={결과경로}`

§6 의 JSON 이 그대로 온다.

### `prj_config` 옵션 (전체)

| 항목명 | 선택 값 | 기본값 |
|---|---|---|
| `extract_type` | `all` / `dla` / `parser`(hwpx·docx 직접파싱) | `dla` |
| `table_to_struct` | `html` / `md` | `html` |
| `tsr_model_name` | `tsr-vis` / `trs-sem` | `tsr-vis` |
| `n_columns` | `1`(단순) / `-1`(알고리즘) | `-1` |
| `rm_overlap_dla_flag` | true / false | `true` |
| `ocr_only` | true(OCR만) / false(OCR+pdf reader) | `false` |
| `adjusting_process` | true / false (북마크 사용) | `false` |
| `merge_unknown_type` | true / false | `true` |
| `draw_viz` / `draw_tsr` / `draw_sort` | true / false (시각화 파일 생성) | `true`/`true`/`false` |
| `dla_score_th` | 0.0 ~ 1.0 | `0.5` |
| `str_score_th` | 0.0 ~ 1.0 | `0.5` |
| `start_page` / `end_page` | 정수 | `0` / `-1` |

광고물에 직접 걸리는 것은 `table_to_struct`(html 유지), `ocr_only`, `dla_score_th`
셋이다. **기본값으로 먼저 실측하고 근거 없이 건드리지 않는다.**

## 6. 결과 JSON 구조 (`res_type: default`)

```text
pdfName   STR   입력 파일 이름
pageLen   INT   페이지 수
pages     LIST
 ├ pageId / width / height     INT   (width·height 는 "이미지 변환 후" 크기)
 ├ paragraphs  LIST
 │  ├ paragraphId   INT
 │  ├ type          STR   Title / List-item / Equation / Figure / Table /
 │  │                     PageHF / Index / Text / Unknown   (9종)
 │  ├ bbox          4점 폴리곤 [[x,y],[x,y],[x,y],[x,y]]
 │  ├ contents      STR   영역 안 텍스트 병합 결과 (표면 HTML)
 │  ├ confidence    FLOAT DLA 검출 신뢰도 (글자 인식 신뢰도가 아니다)
 │  ├ lines         LIST  (Optional) 줄 단위. table·figure·equation 에는 안 온다
 │  │  ├ bbox       4점 폴리곤
 │  │  └ contents   STR
 │  ├ rows / cols   INT   (표일 때) 행·열 수
 │  ├ bullet_id / parentId / childId / error_type / section_level
 │  └ cells         LIST  (표일 때)
 │     ├ bbox       4점 폴리곤
 │     ├ contents   STR
 │     ├ cellId     STR
 │     ├ confidence FLOAT (현재 항상 -1)
 │     └ cell_info  [row_start, row_end, col_start, col_end]
 ├ list_hierarchy    STR
 ├ warning_messages  LIST  예) {"code": "USE_OCR_PAGE", "detail": {}}
 └ n_cols            INT
```

실제 예시 (가이드 p.56, 3x3 표):

```json
{ "paragraphId": 10, "type": "Table",
  "bbox": [[149,776],[1126,776],[1126,909],[149,909]],
  "contents": "<table>\n <tr>\n  <td>나는 표 테이터</td>\n </tr>\n</table>",
  "confidence": 0.999, "lines": [], "bullet_id": "", "parentId": [],
  "error_type": [], "rows": 3, "cols": 3, "childId": [],
  "cells": [ { "bbox": [[153,776],[474,776],[474,822],[153,822]],
               "contents": "나는 표 테이터", "cellId": "C0",
               "confidence": -1, "cell_info": [0,0,0,0] } ] }
```

## 7. 이미 만들어 놓은 것

| 파일 | 내용 | 상태 |
|---|---|---|
| `src/nh_parser/ocr/etlwithllm.py` | 결과 JSON → 우리 데이터 구조 변환기 + 좌표 스케일 변환 | 완료 |
| `src/nh_parser/integrations/etlwithllm/` | 외부 API·DLA 결과 결합·P1/P3 생성 공통 코드 | 팀장 검토용 프로토타입 |
| `integrations/etlwithllm-review/` | 외부 API / transform / CUSTOMIZE 후보와 KL 비교 README | 팀장 검토용 |
| `tests/test_etl_dla_convert.py` | 위 변환기 테스트 15건 (가이드 예시를 픽스처로) | 통과 |
| `src/nh_parser/layout/regions.py` | `_LABEL_TO_ROLE` 에 ETL 타입 5종 추가 | 완료 |
| `tools/etl_probe.py` | 현장 반입용 호출 스크립트. **표준 라이브러리만** (폐쇄망에서 pip 불가) | 완료·리허설 검증 |
| `tools/etl_mock.py` | 방문 전 리허설용 목 서버 | 완료 |
| `docs/농협-ocr-연동-규격.md` | 가이드 58쪽 정리 (부록에 나머지 API 전부) | 완료 |
| `docs/농협-ocr-전환-계획.md` | 격차 11건 · 전환 설계 · 회신 질문 | 완료 |
| `docs/농협-ocr-현장점검.md` | 현장 절차 · 반입물 · 시험 항목 T1~T11 | 완료 |

공개 API 클라이언트는 방향 검토가 가능하도록 프로토타입으로 추가했다. 로그인·워크스페이스
생성은 구현하지 않고, 농협에서 받은 `ws_id`와 인증된 세션을 주입한다. 실제 폐쇄망 원응답
확보용 도구는 별도의 `tools/etl_probe.py`다.

## 8. 확정된 사실

- 원본 파일을 multipart 로 그대로 올린다 (p.28 `upfiles` 는 `List[file]`, p.31 curl 예시)
- 렌더·페이지분리·텍스트레이어 판정을 플랫폼이 내부에서 한다 (§4 표의 근거)
- 입력·출력 스키마와 실제 예시가 가이드에 다 있다 (p.28~30, p.55~57)
- `ws_id` 는 AgileSoDA 가 만들어 전달한다 → 워크스페이스 API 는 우리가 안 쓴다
- `bbox` 는 `[x0,y0,x1,y1]` 이 아니라 **4점 폴리곤**이다. 우리 `_norm_bbox` 가 이미 처리한다
- 표는 `cell_info` 가 행·열 범위를 직접 준다 → PaddleX 경로의 HTML 파싱 추론보다 낫다

## 9. 아직 모르는 것 — 추측으로 메우지 말 것

**차단 항목 (없으면 호출 자체가 안 된다)**

1. 접속 주소(IP:Port)
2. 인증 필수 여부. 가이드에 "인증 미들웨어 설정이 필요한 경우 사용하는 로그인 API"
   라고 되어 있고, 응답 예시의 `created_by` 가 `anonymous` 다 → **꺼져 있을 가능성이 크다.**
   일단 인증 없이 시도하고 401/403 이면 로그인한다.
3. `ws_id` (전달 대기)

**품질·설계에 영향 있는 것**

4. **이미지(PNG/JPG) 업로드 가능 여부.** 가이드 58쪽에 이미지 언급이 **0회**다.
   예시는 전부 PDF 고 샘플 코드는 MIME 을 `application/pdf` 로 고정한다.
   PDF·HWPX·DOCX 는 근거가 있다(`extract_type: parser` 설명).
   **우리 광고물 상당수가 PNG 단건**이라 안 되면 PDF 변환 단계를 넣어야 한다.
5. 세로로 긴 이미지(실측 1122×6429)를 넣으면 내부에서 축소되는지
6. 파일 크기·페이지 수 상한, 동시 요청 수, 처리 시간
7. 에러 코드 정의표. 관측된 값은 `0`(성공), `-1`, `-100` 뿐이다
8. `chunk_status: 999`(오류) 일 때 원인 조회 방법
9. 농협 내부 VLM 제공 여부 (이 가이드 범위 밖)
9-1. **`custom_extension.extract()` 로 전처리 후 DLA 를 이어 부를 수 있는가.**
    가이드 3장(p.51~54)에 플랫폼 확장 훅이 있다.
    - `extract(input_path, output_path, digitize_config)` — `ExtractType.CUSTOMIZE` 일 때만
      호출된다. 원본 파일 경로를 받으므로 **우리 전처리를 플랫폼 안에서 살릴 수 있는
      자리**다.
    - 그런데 p.53 분기에서 `CUSTOMIZE` 는 `dla_task()` 를 부르지 않는다. 전처리 후 DLA 로
      이어지지 않으면, 이 훅을 쓰는 순간 OCR 도 우리가 들고 들어가야 한다(= 농협 OCR 을
      쓰라는 요구와 어긋난다).
    - `transform(input_file_path, dla_result_path, ...)` 은 `dla_result_path` 를 받으므로
      **DLA 결과 후처리는 확실히 가능**하다(p.54).
    - 부수 확인 사항: `transform()` 예시가 부르는 `res_type` 목록에 `"simple"` 이 있는데
      (p.54) `res_type` 표(p.29·p.39)에는 없다. 또 API 의 `prj_config`(p.28)가
      `extract()` 의 `digitize_config`(p.53)와 같은 값인지 불명확하다.
    - 이 답에 따라 전환 계획 §5.4 의 A(외부 호출) / B(플랫폼 내부 이식) 선택이 갈린다.

**우리 하류에 필요한데 "제공하지 않는다" 고 나온 것 — 대응 방침 필요**

10. **줄 단위 인식 신뢰도가 없다.** `confidence` 는 문단 단위 DLA 검출 점수다.
    영향: `review/export.py` 의 `needs_review` 판정(임계값 0.80),
    저신뢰 줄 재판독, `ocr/tiling.py` 의 중복 제거 우선순위.
    **임계값 0.80 을 그대로 옮기면 안 된다 — 다른 물리량이다.**
11. **표 영역에 줄(`lines`)이 안 온다.** 그런데 우리 설계는 표의 정본 텍스트를
    `Region.lines` 에 두고 셀 텍스트는 근거로만 쓴다(`ir.py::TableCell` 주석에
    실측 근거 있음: 표 안 글자에도 OCR 오류가 있다).
    → 정본을 `cells[].contents` 로 올릴지 결정해야 한다. **현장 실물을 보고 정한다.**

**가이드 자체의 표기 모순 (구현 전 확인 필요)**

12. `doc_result` 깊이 — 표(p.57)는 `doc_result → pdfName`, 예시(p.48)는
    `doc_result → default → pdfName`
13. `result_doc_path` 가 문자열인지 리스트인지 (p.48 vs p.57)
14. `author` 필수 여부 — 표(p.28)는 필수, 파이썬 샘플(p.46)에는 없음
15. `1.20` 의 `doc_path` 가 입력 경로인지 결과 경로인지 (p.38 vs p.39)
16. `tsr_model_name` 값이 `trs-sem` 인지 `tsr-sem` 인지 (오타로 보인다)

## 10. 남은 작업

**지금 가능 (답변 불필요)**

- 비교 기준선 산출: 샘플 4~5건을 현재 PaddleX 경로로 돌려 `out/` 에 남긴다.
  샘플은 성격이 달라야 한다 — 이미지 단건 / 세로로 긴 광고 / 표 포함 /
  텍스트레이어 있는 PDF / 작은 글씨(유의사항·심의필) 포함
- 목 서버로 `etl_probe.py` 리허설 재확인

**접속 정보 확보 후 (현장)**

- `etl_probe.py run` 으로 원응답 JSON 확보 ← **현장의 유일한 필수 산출물**
- `etl_probe.py summary` 로 계약 점검 (줄 신뢰도 / 표 lines / cell_info / bbox 형태)

**현장 후 (사무실)**

- 원응답으로 `tests/test_etl_dla_convert.py` 픽스처를 실물로 교체
  (지금 픽스처는 가이드 예시라 추측이 섞여 있다)
- `cell_info` 가 양끝 포함인지 확정 (지금은 3x3 표 첫 셀 `[0,0,0,0]` 에서 유추한 해석)
- 변환기 보정 → 영역 조립까지 통과시키기
- PaddleX 대조: 골드 문장 회수율(현재 기준선 236/242), 표 구조 유지, fine-print 회수
- 9번 10·11 항목 방침 확정
- `OCR_PROVIDER=paddlex|etl` 분기 추가. `ocr/paddlex.py` 는 A/B 기준선으로 남긴다

## 11. 작업 규칙 (이 저장소 관례)

- `CLAUDE.md` 를 따른다: 추측하지 말고 물어본다. 최소 코드. 요청 범위만 건드린다.
- **임계값·파라미터에는 실측 근거를 주석으로 남긴다.** `config.py` 를 보면 모든 숫자에
  왜 그 값인지가 적혀 있다. 근거 없이 값을 바꾸지 않는다.
- 문서는 한국어. 기존 `docs/` 문체(~한다)를 따른다.
- 사내 엔드포인트를 코드에 하드코딩하지 않는다. `.env` 에서 읽는다
  (저장소가 공개된 사고 이력이 있다).
- `probe-out/` 은 `.gitignore` 대상이다. **농협 문서 원응답이 들어가므로 커밋 금지.**
- 테스트: `uv run pytest`. 현재 203 통과 / 37 건너뜀(샘플·네트워크 의존).

## 12. 참고 문서

| 파일 | 언제 보나 |
|---|---|
| `docs/농협-ocr-연동-규격.md` | API 세부 스펙, 나머지 API 전체, 에러 형식 |
| `docs/농협-ocr-전환-계획.md` | 격차 11건 상세, 전환 설계, 농협에 보낼 질문 |
| `docs/농협-ocr-현장점검.md` | 현장 절차, 시험 항목 T1~T11 |
| `docs/출력-계약.md` | P1/P3 출력 계약 (이건 바뀌지 않는다) |
| `docs/서빙-구조-개념.md` | 현재 PaddleX·VLM 서빙 구조 |
| 원본 PDF | `C:\Users\cccjj\Downloads\[AgileSoDA] ETLwithLLM API가이드_v1.1.0.pdf.pdf` (텍스트 레이어 없음. 위 문서들로 충분하다) |
