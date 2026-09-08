# 농협 OCR(ETL with LLM) 연동 규격

출처: `[AgileSoDA] ETLwithLLM API가이드_v1.1.0.pdf` (58쪽, 2026-09-07 수신).

원본은 **텍스트 레이어가 없는 이미지 PDF** 다(`pdftotext` 결과 58바이트 = 페이지
구분자뿐). 본문·표는 페이지 렌더로, 화면 캡처로 들어간 요청/응답 예시는 해당 영역만
고해상도로 다시 잘라 판독했다. 이 문서는 그 58쪽을 **우리가 호출할 순서대로** 재배열한
것이며, 원문에 없는 값은 넣지 않는다. 원문이 모순되거나 비어 있는 자리는
[§9 원문의 공백·모순](#9-원문의-공백모순) 에 따로 모았다 — 추측으로 메우지 않는다.

전환 계획·우리 코드와의 격차는 [농협-ocr-전환-계획.md](농협-ocr-전환-계획.md).

---

## 0. 한 줄 요약

받은 것은 **OCR API 가 아니라 비동기 문서분석(DLA) 플랫폼 API** 다.

```text
워크스페이스 준비 → 파일 업로드 + 분석 요청 → (폴링 | callback) → 결과 JSON 조회
```

- 단일 요청으로 "이미지 주면 글자 준다" 가 아니다. **작업(task) 을 만들고 상태를 기다린다.**
- 돌려주는 것은 OCR 텍스트만이 아니라 **레이아웃 분석 결과**(영역 종류 + 4점 좌표 +
  텍스트 + 표 HTML)다. 즉 우리 PaddleX **PP-StructureV3 자리를 통째로 대체**하는 물건이고,
  단순 OCR 엔진 교체가 아니다.
- 인증은 **조건부**다. "인증 미들웨어 설정이 필요한 경우 사용하는 로그인 API" 라고
  적혀 있고, 실제 응답 예시의 `created_by` 가 `anonymous` 다. 농협 환경에서 켜져 있는지는
  확인해야 한다(§8).

---

## 0-1. 용어 — 이 플랫폼 안에는 모델이 여러 개다

가이드가 약어로 구분해 쓴다. **이걸 구분하지 않으면 "OCR 인데 왜 신뢰도가 없나" 를
설명할 수 없다.**

| 약어 | 뜻 | 하는 일 | 근거 |
|---|---|---|---|
| **DLA** | Document Layout Analysis | 영역 검출 + 종류 분류(9종) | p.1 "DLA(Document Layout Analysis) 분석 결과", p.30 `dla_score_th` |
| **STR** | Scene Text Recognition | **글자 인식** (= 좁은 의미의 OCR) | p.30 `str_score_th` "STR 결과 confidence threshold 설정" |
| **TSR** | Table Structure Recognition | 표 행·열 구조 인식 | p.29 `tsr_model_name`, p.30 `draw_tsr` |
| **CD** | Cell Detection | 표 셀 검출 | p.56 `cells[].confidence` = "CD 모델 confidence (현재는 -1)" |

`prj_config` 에 `dla_score_th` 와 `str_score_th` 가 **따로** 있는 것이 이 둘이 별개
모델이라는 증거다(p.30).

**결과 JSON 이 노출하는 `confidence` 는 DLA(영역 검출) 신뢰도뿐이다.** STR(글자 인식)
신뢰도는 내부에서 `str_score_th` 로 걸러내는 데 쓰지만 결과에 담아 주지 않는다.
전환 계획의 G1(줄 단위 신뢰도 없음)이 정확히 이 이야기다.

우리 쪽과 대응시키면 짝이 하나가 아니라 셋이다 — 그래서 이 전환은 "OCR 엔진 교체" 가
아니라 **PP-StructureV3 계층 전체 교체**다.

```text
PaddleX PP-StructureV3 =  레이아웃 검출  +  PP-OCR(글자 인식)  +  표 인식
ETL with LLM           =  DLA           +  STR               +  TSR / CD
```

---

## 1. API 전체 목록 (23개)

`*` = 우리 광고심의 파이프라인에 필요한 것.

| # | API | 메서드 | 경로 |
|---|---|---|---|
| 1.1 | 사용자 로그인 | POST | `/api/v1/user/login` |
| 1.2 | 사용자 비밀번호 변경 | POST | `/api/v1/user/change_pwd` |
| 1.3 | 사용자 계정 생성·수정 | POST | `/api/v1/user` |
| 1.4 | 팀 생성·수정 | POST | `/api/v1/user/team` |
| 1.5 | 팀 목록 조회 | GET | `/api/v1/user/team` |
| 1.6 | 특정 팀 사용자 목록 | GET | `/api/v1/user/member?teamId={id}` |
| 1.7 | 사용자 활동 여부 목록 | GET | `/api/v1/user/active` |
| 1.8 | 사용자 삭제 | POST | `/api/v1/user/delete` |
| 1.9 | 팀 삭제 | POST | `/api/v1/user/team/delete` |
| 1.10 | 워크스페이스 생성 | POST | `/api/v1/workspace` |
| 1.11 | 워크스페이스 목록 조회 | POST | `/api/v1/workspace/list` |
| 1.12 | 특정 워크스페이스 정보 | GET | `/api/v1/workspace/{workspace_id}` |
| 1.13 | 워크스페이스 내 파일 목록 | GET | `/api/v1/file/list?wsId={id}` |
| 1.14 | 워크스페이스 삭제 | DELETE | `/api/v1/workspace/{id}` |
| 1.15 | 워크스페이스 내 전체 파일 삭제 | DELETE | `/api/v1/workspace/{id}/files` |
| **1.16*** | **DLA 분석 요청 (업로드 동시)** | POST | `/api/v1/etl/auto/start` |
| **1.17*** | **파일 분석 상태 확인** | GET | `/api/v1/file/info?file_path={경로}` |
| **1.18*** | **분석 결과 파일 목록** | GET | `/api/v1/file/result/list?docPath={경로}` |
| **1.19*** | **분석 결과 내용 요청** | GET | `/api/v1/file/result/doc?docResultPath={경로}` |
| **1.20*** | **분석 결과 내용 (by Type)** | GET | `/api/v1/file/result/json/type?doc_path={경로}&res_type={타입}` |
| 1.21 | 분석 중지 | POST | `/api/v1/etl/stop/task` |
| 1.22 | 업로드된 파일 재분석 | POST | `/api/v1/etl/start` |
| 1.23* | 분석 결과 삭제 | POST | `/api/v1/file/delete/datasrc` |

호스트·포트는 문서에 `<IP>:<Port>` 로만 나온다. 다만 에러 예시 캡처에 개발 서버가
그대로 노출돼 있다 — `http://192.168.100.170:58000/api/v1/...`. **포트 58000 이
기본값으로 보이나 농협 배포 주소는 별도로 받아야 한다.**

---

## 2. 우리가 쓸 최소 호출 흐름

> **2026-09-08 담당자 회신으로 확정:** `ws_id`(워크스페이스)는 **AgileSoDA 측이 생성해
> 전달**한다. 따라서 워크스페이스 생성·조회 API(1.10~1.15)와 계정·팀 API(1.1~1.9)는
> **우리가 호출할 일이 없다.** 우리가 실제로 쓰는 것은 아래 4개뿐이다.

```text
[선행] ws_id 를 받는다 (우리가 만들지 않는다)

[문서 1건마다]
  1) POST /api/v1/etl/auto/start          multipart: tr_data(JSON 문자열) + upfiles
       → data.task_ids[]  data.file_paths[]   ex) "test1.pdf/v1/test1.pdf"
  2) GET  /api/v1/file/info?file_path={file_paths[i]}
       → data.datasource[0].chunk_status 가 "002" 가 될 때까지 폴링
         ("999" 면 실패, "001" 진행중, "000" 초기)
  3) GET  /api/v1/file/result/list?docPath={file_paths[i]}
       → data[].file_path 중 "{파일명}.json" 을 고른다
  4) GET  /api/v1/file/result/doc?docResultPath={그 경로}
       → Default JSON 결과 (§6)
```

`callback_url` 을 주면 2~4 를 건너뛰고 결과가 밀려온다(§7). 폐쇄망에서 우리 쪽
수신 엔드포인트를 열 수 있는지에 따라 갈린다 — 열리지 않으면 **폴링이 유일한 길**이다.

`file_path` 형식은 `[파일명]/[버전번호]/[파일명]` 이다. 예: `test1.pdf/v1/test1.pdf`.
결과 JSON 경로는 `test1.pdf/v1/test1.json`.

---

## 3. 1.16 DLA 분석 요청 (핵심)

```text
POST /api/v1/etl/auto/start
Content-Type: multipart/form-data
Accept: application/json
```

form-data 필드는 **두 개뿐**이다.

| Key | Type | 설명 | 필수 |
|---|---|---|---|
| `tr_data` | Text (JSON 문자열) | 분석 요청 데이터. JSON 을 문자열로 인코딩해 전송 | 필수 |
| `upfiles` | List[file] | 업로드할 파일 리스트. **1개 이상** | 필수 |

### `tr_data` 내부 필드

| 항목 | 타입 | 설명 | 필수 | 기본값 |
|---|---|---|---|---|
| `author` | str | 사용자 이름 | 필수 | - |
| `ws_id` | str | 워크스페이스 ID | 필수 | - |
| `callback_url` | str | 분석 완료 후 콜백 받을 URL | 선택 | NULL |
| `res_type` | str 또는 list | 콜백 응답 타입 | 선택 | `default` |
| `prj_config` | dict | 분석 설정 | 선택 | 기본 config |
| `meta_info` | dict | 부가 메타 데이터 (콜백 결과에 그대로 실려 돌아온다) | 선택 | NULL |

```json
{
  "author": "{author}",
  "ws_id": "{ws_id}",
  "callback_url": "http://{server-ip}:{port}/{callback-endpoint}",
  "res_type": "default",
  "prj_config": {},
  "meta_info": {}
}
```

curl 예시(원문 p.31)에서는 `res_type` 을 리스트로 준다 — `"res_type": ["default"]`.
파이썬 예시(p.47)도 `res_type = ["default", "pages"]` 로 **리스트**다. 문자열/리스트
둘 다 받는다는 표 설명과 일치한다.

### 응답

```json
{
  "result": { "code": 0, "message": "success" },
  "meta": null,
  "data": {
    "task_ids": [
      "39d39765-1711-46e1-858d-417f0f488ef8",
      "843c0d20-f531-4cbf-ac8f-00c65ce6480e",
      "91c1d32d-c9c6-42a3-9508-1136bb2e74ac"
    ],
    "file_paths": [
      "test1.pdf/v1/test1.pdf",
      "test2.pdf/v1/test2.pdf",
      "test3.pdf/v1/test3.pdf"
    ],
    "meta_info": {},
    "msg": "add tasks"
  }
}
```

`task_ids` 와 `file_paths` 는 업로드한 파일 순서대로 병렬 배열이다. 이후 조회는
**`task_id` 가 아니라 `file_path` 로** 한다(1.17~1.20 전부). `task_id` 는 분석 중지
(1.21) 에만 쓴다.

---

## 4. `prj_config` — 분석 옵션 전체

기본값을 쓸 경우 `prj_config` 자체를 안 보내도 된다.

| 설정 | 항목명 | 선택 값 | 기본값 |
|---|---|---|---|
| 추출 모드 | `extractType` | `all`(dla+직접파싱) / `dla` / `parser`(hwpx·docx 직접파싱) | `dla` |
| 테이블 구조 | `table_to_struct` | `html` / `md` | `html` |
| 테이블 인식 모델 | `tsr_model_name` | `tsr-vis` / `trs-sem` | `tsr-vis` |
| 정렬 방식 | `n_columns` | `1`(단순) / `-1`(알고리즘) | `-1` |
| 과밀집 항목 제거 | `rm_overlap_dla_flag` | `true`(제거) / `false` | `true` |
| 모델 결과만 사용 | `ocr_only` | `true`(OCR만) / `false`(OCR + pdf reader) | `false` |
| 북마크 사용 | `adjusting_process` | `true` / `false` | `false` |
| Unknown 타입 병합 | `merge_unknown_type` | `true` / `false` | `true` |
| 시각화 파일 (viz) | `draw_viz` | `true` / `false` | `true` |
| 시각화 파일 (tsr) | `draw_tsr` | `true` / `false` | `true` |
| 시각화 파일 (sort) | `draw_sort` | `true` / `false` | `false` |
| DLA 신뢰값 기준 | `dla_score_th` | `0.0` ~ `1.0` | `0.5` |
| STR 신뢰값 기준 | `str_score_th` | `0.0` ~ `1.0` | `0.5` |
| 분석 시작 페이지 | `start_page` | 정수 | `0` |
| 분석 끝 페이지 | `end_page` | 정수 | `-1` |

광고물 파싱에 직접 걸리는 값은 셋이다.

- `table_to_struct: "html"` — 우리 `RegionTable.html` 이 그대로 받는 형식. 기본값 유지.
- `ocr_only` — 광고물은 PDF 텍스트 레이어를 못 믿는 입력이 많다(스캔·이미지 PDF).
  우리 triage 가 이미 그 판정을 하고 있으므로 **트랙에 맞춰 켜고 끄는 후보**다.
- `dla_score_th` / `str_score_th` — 낮추면 작은 fine-print 를 더 건지고 오검출이 는다.
  우리가 PaddleX 에서 `text_det_limit_side_len` 으로 잡던 문제와 성질이 같다.
  **기본값 0.5 로 실측한 뒤 조정할 것.** 근거 없이 건드리지 않는다.

`draw_*` 는 시각화 산출물 생성 여부다. 배치 처리 속도에 영향을 주므로 운영에서는
끄는 쪽을 검토한다(문서에 성능 수치는 없다).

---

## 5. `res_type` — 결과 파일 종류

| res_type | 전달 파일 | 설명 |
|---|---|---|
| `default` | `[파일명].json` | DLA 분석 기본 JSON 결과 |
| `pages` | `[파일명]_pages.json` | 전체 페이지 contents 결과 |
| `page_grouped` | `[파일명]_page_grouped.json` | 페이지 기준으로 그룹화한 결과 |
| `figure` | crop image | crop 된 이미지 파일 (multipart 로 json+file 전달) |
| `figure_base64` | crop image (binary) | 이미지를 binary 형태로 전달 |
| `chunk_data` | `[파일명]_chunk_data.json` | 약관·사업방법서·산출방법서(절·관·조 문서) chunk 결과 |

**우리에게 필요한 것은 `default` 뿐이다.** 좌표가 있는 유일한 형식이기 때문이다.
`page_grouped` 는 좌표 없이 문자열 배열만 준다.

```json
{ "page_grouped": [ { "page": 1, "contents": [ "Diverse Structured Test Document 1 - Page 1", "1. Main Section 1 on Page 1" ] } ] }
```

`chunk_data` 는 **규정문서(약관) 적재 트랙**의 물건이다 — 광고물이 아니라
Knowledge Lake 쪽에서 쓸 값이니 혼동하지 않는다.

---

## 6. Default JSON 결과 구조 (우리가 파싱할 대상)

```text
pdfName        STR    입력 PDF 파일 이름
pageLen        INT    PDF 의 페이지 수
pages          LIST
 ├ pageId      INT    페이지 아이디
 ├ width       INT    이미지 변환 후 너비
 ├ height      INT    이미지 변환 후 높이
 ├ paragraphs  LIST   DLA 에서 인식한 객체 리스트
 │  ├ paragraphId   INT
 │  ├ type          STR    Title / List-item / Equation / Figure / Table /
 │  │                      PageHF / Index / Text / Unknown
 │  ├ bbox          LIST[INT]  DLA 예측 좌표 (이미지 기준)
 │  ├ contents      STR    객체 안 텍스트 병합 결과
 │  ├ confidence    FLOAT  DLA 예측 confidence
 │  ├ lines         LIST   (Optional) table·image·equation 외 클래스의 줄 단위 병합
 │  │  ├ bbox       LIST[INT]
 │  │  └ contents   STR
 │  ├ bullet_id     STR
 │  ├ rows / cols   INT    (Optional) table 일 때 행·열 수
 │  ├ parentId      LIST[str]  table/figure/equation 일 때 부모 테이블의 [paragraphId_cellId]
 │  ├ childId       LIST[str]  table 일 때 자식 테이블의 paragraphId
 │  ├ error_type    LIST[str]
 │  ├ pageId        INT    (Optional)
 │  ├ section_level INT    (Optional)
 │  └ cells         LIST[Cell]  (Optional) table 일 때 탐지된 셀 목록
 │     ├ bbox       LIST[INT]  TSR 셀 좌표 (이미지 기준)
 │     ├ contents   STR    셀 안 텍스트 병합 결과
 │     ├ cellId     STR    정렬된 셀 순서
 │     ├ confidence FLOAT  CD 모델 confidence (현재는 -1)
 │     └ cell_info  LIST[INT]  [row_start, row_end, col_start, col_end]
 ├ list_hierarchy      STR
 ├ warning_messages    LIST[{code, detail}]   ex) {"code": "USE_OCR_PAGE", "detail": {}}
 └ n_cols              INT
```

### bbox 는 4점 폴리곤이다

원문 p.38 실제 결과 캡처:

```json
{ "paragraphId": 0, "type": "Table",
  "bbox": [ [25, 29], [1621, 29], [1621, 315], [25, 315] ] }
```

즉 `[x0,y0,x1,y1]` 이 아니라 **`[[x,y] × 4]`** 다. 우리 `ir` 은 `[x0,y0,x1,y1]` 을
쓰지만 `ocr/paddlex.py::_norm_bbox` 가 이미 폴리곤을 받아 처리한다 — 변환 자체는
새로 만들 필요가 없다.

### 표 예시 (p.56)

```json
{ "paragraphId": 10, "type": "Table",
  "bbox": [[149,776],[1126,776],[1126,909],[149,909]],
  "contents": "<table>\n <tr>\n  <td>나는 표 테이터</td> </tr>\n</table>",
  "confidence": 0.999, "lines": [], "bullet_id": "", "parentId": [],
  "error_type": [], "rows": 3, "cols": 3, "childId": [],
  "cells": [ { "bbox": [[153,776],[474,776],[474,822],[153,822]],
               "contents": "나는 표 테이터", "cellId": "C0",
               "confidence": -1, "cell_info": [0, 0, 0, 0] } ] }
```

표는 `contents` 에 HTML, `cells` 에 좌표 + `cell_info`(행·열 범위)가 함께 온다.
**PaddleX 의 `pred_html` + `cell_box_list` 조합보다 정보가 많다** — 우리는 HTML 셀
개수와 좌표 개수가 안 맞으면 좌표를 버리는 폴백을 두고 있는데(`_build_table_grid`),
여기서는 `cell_info` 가 행·열을 직접 주므로 그 폴백이 필요 없다.

---

## 7. Callback JSON 구조

```text
file_name        STR   입력 PDF 파일 이름
result_doc_path  LIST  결과 파일의 경로 리스트
task_result      DICT
 ├ result           STR  분석 결과 상태 값
 ├ doc_path         STR  파일 경로
 └ processing_time  STR  분석 소요 시간
meta_info        DICT  요청 시 넣은 값이 그대로 돌아온다
doc_result       DICT  분석 결과 (하위 구조는 default JSON 과 같다)
```

원문 p.48 실제 예시 (res_type `default`):

```json
{
  "file_name": "test_document.pdf",
  "result_doc_path": "test_document.pdf/v4/test_document.pdf/test_document.json",
  "task_result": {
    "result": "success",
    "doc_path": "test_document.pdf/v4/test_document.pdf",
    "processing_time": "2.8357679844s"
  },
  "meta_info": {},
  "doc_result": { "default": { "pdfName": "test_document.pdf", "pageLen": 2, "pages": [] } }
}
```

콜백 수신 서버는 `application/json` 과 `multipart/form-data`(`json_data` 필드 +
`files`) 를 모두 받을 수 있어야 한다 — `res_type` 에 `figure` 가 있으면 multipart 로
온다. 원문 p.50~51 에 FastAPI 예제가 있다.

---

## 8. 인증

- 로그인: `POST /api/v1/user/login` `{user_id, user_pw}` → **`Set-Cookie` 헤더에 토큰**.
- 이후 요청은 `Cookie: token=...` 로 자동 포함된다.
- 파이썬 예제는 `response.cookies.get("token")` 으로 꺼내 `cookies={"token": token}` 로 넘긴다.
- API Key·Bearer 토큰 방식은 문서에 없다.

**단, 상시 필수가 아니다.** 1.1 설명이 "인증 미들웨어 설정이 필요한 경우 사용하는
로그인 API" 이고, 1.17 응답 예시의 `created_by`/`updated_by` 가 `anonymous` 다.
농협 배포 인스턴스에서 미들웨어가 켜져 있는지 확인이 필요하다.

권한 등급은 `role` 코드로 나뉜다 — `001` admin / `002` leader / `003` member / `004` user.
워크스페이스 `access_level` 은 `001` public / `002` private.

---

## 9. 원문의 공백·모순

추측으로 메우지 않고 **되물어야 할 것**들이다. 전환 계획의 질문 목록과 짝이 된다.

### 9.1 모순 — 구현 전에 확인해야 하는 것

| # | 내용 | 근거 |
|---|---|---|
| M1 | `doc_result` 의 깊이. 표(p.57)는 `doc_result → pdfName` 인데, 실제 예시(p.48)는 `doc_result → default → pdfName` 으로 **res_type 키가 한 겹 더 있다**. 콜백 파서가 갈린다 | p.57 표 vs p.48 예시 |
| M2 | `result_doc_path` 타입. 표는 LIST, p.48 예시는 문자열 하나, p.57 예시는 png 경로 리스트 | p.57 표 vs p.48 |
| M3 | 1.20 의 `doc_path` 가 **입력 파일 경로**인지 **결과 파일 경로**인지. URL 설명은 `{결과 파일 경로}`, 파라미터 표는 "DLA 분석 요청 API 에서 생성된 파일 경로"(= 입력 경로) | p.38 vs p.39 |
| M4 | `tsr_model_name` 의 두 번째 값이 `trs-sem` 인지 `tsr-sem` 인지 (원문 표기가 `trs-`, 오타로 보인다) | p.29 |
| M5 | 2.2 절이 "**1.5.** DLA 분석 요청 API" 를 참조한다. 실제 DLA 요청은 1.16 이다 | p.50 |
| M6 | **`author` 가 필수인지.** `tr_data` 표는 `author` 를 **필수**로 적었는데, 공식 파이썬 샘플의 `tr_data` 에는 `author` 가 **없다**(`ws_id`/`callback_url`/`res_type`/`meta_info` 만). curl 예시에는 있다 | p.28 표·p.31 curl vs p.46 코드 |

### 9.2 공백 — 문서에 아예 없는 것

- **입력 파일 형식 목록.** 예시가 전부 PDF 다. `file_type` 필드는 있으나 허용 값이 없다.
  **PNG/JPG 를 그대로 넣을 수 있는지 불명** — 우리 광고물 입력의 상당수가 이미지다.
- **파일 크기·페이지 수 상한, 동시 요청 수, 타임아웃, 처리 시간 기준.**
- **에러 코드 목록.** 관측된 값만 있다 — `0`(성공), `-1`, `-100`. 코드별 의미 정의표가 없다.
- `chunk_status: "999"`(분석 오류) 일 때 **원인을 조회하는 방법**. `pipeline_log.log` 가
  결과 파일 목록에 보이지만 이를 읽는 규격은 없다.
- **재시도·중복 업로드 정책.** 같은 파일명을 다시 올리면 버전이 오르는지(`/v1/`, `/v4/`
  가 예시에 보인다) 규칙이 없다.
- **좌표계의 원점·단위.** "이미지 변환 후 너비/높이" 라고만 하고 렌더 DPI 를 밝히지 않는다.
  예시가 1275×1650 과 1650×1275 로 엇갈린다(= letter 150dpi 의 세로/가로).

### 9.3 에러 응답 — 형식이 **두 가지** 섞여 있다

플랫폼 표준형:

```json
{ "result": { "code": -100,
              "message": "Failed to execute: POST: http://192.168.100.170:58000/api/v1/workspace/. Detail: (pymysql.err.IntegrityError) (1062, Duplicate entry for key workspace_name) [SQL: INSERT INTO workspace ...]" },
  "meta": { "version": "v1" }, "data": {} }
```

FastAPI 원형(파라미터 누락 시):

```json
{ "detail": [ { "type": "missing", "loc": ["query", "wsId"], "msg": "Field required", "input": null } ] }
```

`{"detail": "Method Not Allowed"}` 형태도 있다. **클라이언트는 `result.code` 와
`detail` 을 모두 볼 수 있어야 한다.** 그리고 표준형 `message` 에는 내부 IP·SQL·
파라미터 값이 그대로 실려 온다 — 로그에 남길 때 주의한다.

관측된 코드:

| code | 상황 |
|---|---|
| `0` | 성공 |
| `-1` | user not exists / password error / "해당 res_type 에 해당하는 파일이 존재하지 않습니다." |
| `-100` | 서버 실행 실패 전반 (`Failed to execute: ...` + Detail) |

`chunk_status` (분석 진행 상태): `000` 초기 / `001` 진행중 / `002` 완료 / `999` 오류.
`chunk_step` (분석 단계): `000` 준비 / `001` 파일 추출 / `002` 파일 변환.

---

## 10. custom_extension — 플랫폼 안에 우리 로직을 넣는 경로

원문 3장은 API 가 아니라 **ETL 플랫폼의 확장 지점**을 설명한다. 외부에서 호출하는
것 말고 다른 선택지가 있다는 뜻이라 기록해 둔다.

```text
app/custom_extension/
├── custom_extension.py          # 추상 클래스 (CustomExtension, CustomRouter)
└── implements/                  # 실제 구현체 (외부 호스트 디렉터리와 마운트)
    ├── base_custom_extension.py
    ├── custom_postprocess.py    # DLA 후처리 로직
    ├── api/ modules/ schemas/ services/
```

| 훅 | 시그니처 | 언제 불리나 | DLA 와의 관계 |
|---|---|---|---|
| `extract()` | `(input_path, output_path, digitize_config)` (p.53) | `ExtractType.CUSTOMIZE` 일 때만 | **DLA 를 안 부른다** |
| `transform()` | `(input_file_path, dla_result_path, exec_args, stop_flag)` (p.54) | DLA 완료 후 | **`dla_result_path` 를 받는다 = DLA 경로 위** |
| `get_custom_routers()` | `→ list[CustomRouter]` (p.54) | 서버 기동 시 | 무관. 커스텀 엔드포인트 등록 |

### ⚠ `CUSTOMIZE` 분기는 `dla_task()` 를 부르지 않는다

p.53 의 `etl_service.py` 발췌를 분기별로 대조하면 드러난다.

```python
if   extract_type == ExtractType.DLA_AND_PARSER:  await dla_task(); await parser_task()
elif extract_type == ExtractType.DLA_ONLY:        await dla_task()
elif extract_type == ExtractType.PARSER_ONLY:     await parser_task()
elif extract_type == ExtractType.CUSTOMIZE:
    if custom_extension := CustomExtension.create():
        await custom_extension.extract(input_file_path, output_dir_path, digitize_config)
        # ← dla_task() 가 없다
```

**그래서 `extract()` 를 "DLA 앞의 전처리 자리" 로 쓸 수 있는지가 불확실하다.** 전처리만
하고 DLA 를 이어 부를 수 없다면, `CUSTOMIZE` 를 쓰는 순간 OCR 도 우리가 들고 들어가야
한다 — 농협 OCR 을 쓰라는 요구와 어긋난다. **애자일소다에 확인해야 하는 항목이다.**

반면 `transform()` 은 `dla_result_path` 를 받으므로 **DLA 결과 후처리는 확실히 가능**하다.
"DLA 결과를 우리 형식으로 바꾸는 일" 만 필요하다면 이 훅으로 된다.

`extract()` 가 `output_path` 를 받는 점도 단서다 — 여기에 DLA 결과 형식으로 파일을 쓰면
이후 단계가 이어지는지 함께 물어볼 것.

### 원문에 없는 것

- `transform()` 예시가 `custom_postprocess_dla(..., ["pages", "page_grouped", "simple"])`
  를 부르는데(p.54) **`simple` 은 `res_type` 표(p.29·p.39)에 없다.**
- API 는 `prj_config` 로 받는데(p.28) `extract()` 에는 `digitize_config` 로 들어온다(p.53).
  같은 값인지 확인이 필요하다.

`docker-compose.yml` 의 `${CUSTOM_EXTENSION_PATH}` 마운트로 코드를 넣는다.
이 경로를 쓰면 우리 광고 파싱 후처리가 **플랫폼 내부에서** 돌고 결과 JSON 종류를
직접 정의할 수 있다. 채택 여부는 전환 계획 §5 에서 다룬다.

---

## 부록 A. 나머지 API 상세

우리 파이프라인이 직접 쓰지 않는 API 다. 계정·워크스페이스 준비와 정리(삭제)에
필요할 때 찾아보기 위해 남긴다. 응답은 전부 `{result:{code,message}, meta, data}` 로
감싸여 오므로 `data` 만 적는다.

### A.1 사용자·팀 (1.1~1.9)

| API | 요청 | `data` 응답 |
|---|---|---|
| 1.1 로그인 `POST /user/login` | `user_id`*, `user_pw`* | `email`, `name`, `site`(number), `service`, `role`, `active_yn`(bool), `teamName`, `site_info`(dict), `default_language`, `external_tokens`(dict) — **토큰은 `Set-Cookie`** |
| 1.2 비밀번호 변경 `POST /user/change_pwd` | `email`*, `current_password`*, `change_password`* | 없음 |
| 1.3 계정 생성·수정 `POST /user` | `user_info{email*, name*, password*, site, active_yn(기본 False), role(기본 004), update_mode(기본 False)}` | 없음. **admin 이상 권한 필요** |
| 1.4 팀 생성·수정 `POST /user/team` | `id`*, `name`*, `active_yn`*, `update_mode`(기본 False) | 없음. **admin 이상** |
| 1.5 팀 목록 `GET /user/team` | 없음 | `[{id, name, active_yn, created_at, updated_at, team_member_len{leader, member}}]`. **leader 이상** |
| 1.6 팀 사용자 목록 `GET /user/member?teamId=` | `teamId`*(int) | `[{active_yn(int), created_at, email, name, role, role_label, service, site(int)}]`. **leader 이상** |
| 1.7 사용자 활동 여부 `GET /user/active` | 없음 | `[{email, name, role}]` |
| 1.8 사용자 삭제 `POST /user/delete` | `user_ids`*: array(string) — 이메일 목록 | 없음 |
| 1.9 팀 삭제 `POST /user/team/delete` | `team_ids`*: array(string) — 팀 id 목록 | 없음 |

`role` 코드: `001` admin / `002` leader / `003` member / `004` user.
1.3 의 에러: 중복 계정 → `-100 ... Detail: DB Statement Error`,
권한 부족 → `-100 ... Detail: Permission is denied.`

### A.2 워크스페이스 (1.10~1.15)

| API | 요청 | `data` 응답 |
|---|---|---|
| 1.10 생성 `POST /workspace` | `workspace_name`*, `description`, `access_level`*(`001` public/`002` private), `created_by`*, `path`* | `{id, workspace_name, description, path, access_level, created_by}` (예시엔 `update_mode` 도 포함) |
| 1.11 목록 `POST /workspace/list` | `search`*, `user_id`* | `[{id, workspace_name, path, created_by, created_at, access_level, description, source_cnt, ing_cnt}]` (예시엔 `complete_cnt` 도 포함) |
| 1.12 단건 조회 `GET /workspace/{id}` | 경로 파라미터 | `[{id, workspace_name, path, creator, created_at, access_level, description}]` |
| 1.13 파일 목록 `GET /file/list?wsId=` | `wsId`* | `{workspace{...}, datasource[{file_id, version_number, file_path, chunk_status, chunk_step, created_by, updated_by, file_metadata, is_active, task_id, id, file_name, workspace_id, file_type, version_id, version_created_at, version_updated_at}]}` |
| 1.14 삭제 `DELETE /workspace/{id}` | 경로 파라미터 | 없음. **안의 파일도 모두 삭제된다** |
| 1.15 전체 파일 삭제 `DELETE /workspace/{id}/files` | 경로 파라미터 | 없음 |

`source_cnt` = 업로드한 Data source 개수, `ing_cnt` = 추출된 Data source 개수.
1.10 은 `workspace_name` 중복 시 실패한다(§9.3 의 IntegrityError 예시가 이 경우다).
1.11 에서 `search` 를 빼면 `-100 ... Detail: 'search'`.
1.13 에서 `wsId` 를 빼면 FastAPI 원형 에러(`{"detail":[{"type":"missing", ...}]}`).
1.14 에서 id 를 빼면 `{"detail": "Method Not Allowed"}`.

### A.3 작업 제어·정리 (1.21~1.23)

| API | 요청 | 비고 |
|---|---|---|
| 1.21 분석 중지 `POST /etl/stop/task` | `taskIds`*: array(string) — worker task id 목록 | 1.16 이 준 `task_ids` 를 쓰는 **유일한 곳** |
| 1.22 업로드된 파일 재분석 `POST /etl/start` | `author`*, `filePath`* | 중지했던 파일을 다시 분석. `data{task_id, msg}` |
| 1.23 분석 결과 삭제 `POST /file/delete/datasrc` | `file_path`*: array(string) | 경로 미지정 시 `-100 "file paths are not found"` |

1.23 은 축적된 광고물 원본 정리에 쓸 API 다(전환 계획 G10).

---

## 부록 B. 공식 예제 코드 요지

### B.1 호출 측 (원문 2.1, p.45~47)

사용자가 채워야 하는 값 6개: `user_id`, `user_pw`, `filenames`(업로드 파일 경로),
`ws_id`, `callback_url`, `res_type`.

```python
# 1) 로그인 — 토큰은 쿠키에서 꺼낸다
response = requests.post(login_url, headers={"Content-Type": "application/json"},
                         json={"user_id": user_id, "user_pw": user_pw})
token = response.cookies.get("token")

# 2) ETL 요청 — tr_data 는 json.dumps 한 문자열, 파일은 upfiles 로 반복 첨부
data = {"tr_data": json.dumps({
    "ws_id": ws_id, "callback_url": callback_url,
    "res_type": res_type, "meta_info": {},
})}
files = [("upfiles", (os.path.basename(f), open(f, "rb"), "application/pdf"))
         for f in filenames]
requests.post(url, data=data, files=files, headers={},
              cookies={"token": token} if token else {}, timeout=30)
```

- 파일 MIME 을 `application/pdf` 로 고정해 넘긴다 — 이미지 지원 여부가 불명한 것과
  맞물리는 대목이다(§9.2).
- `res_type` 은 리스트로 준다: `["default", "pages"]`.
- **`author` 가 빠져 있다** — 표는 필수라고 한다(§9.1 M6).
- `timeout=30` 은 **접수 응답**까지의 시간이다. 분석 완료 시간이 아니다.

### B.2 콜백 수신 측 (원문 2.2, p.50~51)

FastAPI 로 `POST /callback` 하나를 열고 **두 가지 Content-Type 을 모두** 처리한다.

```python
@app.post("/callback")
async def callback_endpoint(
    req: Request,
    json_data: Optional[str] = Form(None),   # multipart 안의 JSON 필드
    files: List[UploadFile] = File(None),    # multipart 안의 파일
):
    content_type = req.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        save_json_to_file(await req.json())
    elif content_type.startswith("multipart/form-data"):
        if json_data:
            save_json_to_file(json.loads(json_data))
        for file in files or []:
            ...  # 파일 저장
    return {"status": "Callback received"}
```

JSON 파일명은 페이로드의 `file_name` 을 그대로 쓴다. `res_type` 에 `figure` 가 있으면
multipart 로 오므로 두 분기가 다 필요하다.
