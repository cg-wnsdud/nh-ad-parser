# nh-ad-parser

농협 광고물에서 문구와 위치를 읽고, 광고 템플릿의 구분값에 맞게 정리하여 다음 심의
단계가 사용할 JSON을 만드는 파싱 파이프라인이다.

이 저장소는 **광고 내용의 적법 여부를 판단하는 심의 엔진이 아니다.** 문서에 무엇이
어디에 적혀 있는지 보존하고, 해당 문구가 어떤 심의 항목에 대응하는지 정리하는 단계까지
담당한다.

## 현재 상태

| 항목 | 현재 구현 |
| --- | --- |
| 입력 | PDF, PNG/JPG, HWP/HWPX |
| OCR·문서 구조 분석 | 사내 DGX-Spark에 서빙된 PaddleX PP-StructureV3 호출 |
| 이미지 보조 판독 | 사내 DGX-Spark에 서빙된 Gemma VLM 호출 |
| 후처리 | 읽기 순서·영역 조립, 광고 템플릿 선택, 구분값 분류 |
| 출력 | 원시 파싱 JSON, 판독 근거 P1, 다음 심의 단계 입력 P3 |
| KL Custom Parser 광고 API | **기존 보고용 프로토타입 구현** — 농협 등록·운영은 미확정 |
| 농협 ETLwithLLM 연동 | **미구현 — 현장 확인 후 구현 예정** |

현재 코드는 회사 개발망의 모델 주소를 사용한다. 농협 폐쇄망에서는 회사 모델에 접근할
수 없으므로 다음 두 부분을 농협 내부 서비스로 바꿔야 한다.

1. PaddleX OCR·레이아웃 분석 → 농협 ETLwithLLM의 DLA/OCR 결과
2. Gemma VLM → 농협 내부 vLLM에 서빙된 사용 가능 모델

제공받은 Custom Parser API 예제의 비동기 통신 방식은 광고용 FastAPI 프로토타입으로
구현해 두었다. 다만 농협용 ETL HTTP 클라이언트, 컨테이너·마운트 설정은 아직 확정된
계약이 아니므로 포함하지 않았다. 기존 구현의 범위는
[KL Custom Parser 기존 구현](docs/kl-parser-기존-구현.md)에 정리했다.

## 전체 파이프라인

```text
광고 파일
  │
  ├─ 형식 판별·페이지 렌더링
  │    ├─ PDF: 텍스트 레이어 상태 확인 후 페이지 이미지 생성
  │    ├─ 이미지: 원본을 캔버스로 사용
  │    └─ HWP/HWPX: 문서 텍스트·표와 내장 이미지 분리
  │
  ├─ OCR·문서 구조 분석
  │    └─ 현재: DGX-Spark PaddleX PP-StructureV3
  │
  ├─ 좌표 복원·중복 제거·영역 조립
  │    └─ 모든 문구를 영역 또는 미배정 문구로 보존
  │
  ├─ VLM 보조 판독
  │    └─ 현재: DGX-Spark Gemma — 분류, 읽기 순서, 저신뢰 문구 보조 판독
  │
  ├─ 농협 광고 템플릿 선택·구분값 분류
  │
  └─ parse / evidence(P1) / review-input(P3) JSON 생성
```

### 1. 입력 적재

`src/nh_parser/ingest/`가 입력 형식을 확인한다.

- 이미지 파일은 원본 크기를 유지해 처리한다.
- PDF는 페이지별로 디지털 텍스트 레이어가 있는지 확인한다. 화면의 글자와 텍스트
  레이어를 함께 활용하는 경우 `hybrid` 경로로 기록한다.
- HWP/HWPX의 텍스트와 표는 디지털 정보로 사용하고, 내장 이미지는 OCR 경로로 보낸다.

### 2. OCR·레이아웃 분석

`src/nh_parser/pipeline.py`가 긴 이미지를 타일로 나눈 뒤
`src/nh_parser/ocr/paddlex.py`를 통해 PaddleX에 요청한다. 응답의 문구, 좌표, 영역 종류,
표 구조를 내부 공통 모델인 `AdDocument`로 정리한다.

현재 이 단계가 회사 DGX-Spark에 의존하는 가장 큰 교체 지점이다.

### 3. 영역과 읽기 순서 복원

`src/nh_parser/layout/`가 타일 좌표를 원본 페이지 좌표로 되돌리고, 겹친 결과를 제거하며,
OCR 문구를 제목·본문·표·고지문구 등의 영역에 배정한다. 어느 영역에도 연결되지 않은
문구는 삭제하지 않고 `unassigned_lines`에 남긴다.

### 4. VLM 보조 판독

`src/nh_parser/vlm/`은 이미지와 OCR 결과를 함께 사용하여 문서 종류, 카드 경계, 읽기
순서와 문구를 보조 판독한다. 현재는 Gemma의 OpenAI 호환
`/v1/chat/completions` 엔드포인트를 호출한다.

농협 내부 모델이 같은 요청 형식, 이미지 입력, JSON Schema 응답을 지원하는지는 현장에서
확인해야 한다. 주소만 변경하면 되는지는 이 호환성 확인 후 확정할 수 있다.

### 5. 템플릿·심의 입력 생성

`src/nh_parser/review/`가 19종 농협 광고 템플릿 중 하나를 선택하고 문구를 회사명,
상품명, 금리, 유의사항 등의 구분값에 연결한다. 연결되지 않은 광고성 문구도 원문 유실을
막기 위해 결과에 보존한다.

## 산출물

| 폴더 | 역할 |
| --- | --- |
| `parse/` | OCR·좌표·영역을 보존한 원시 `AdDocument` |
| `evidence/` | P1. 원시 결과, 판독 근거, 템플릿 선택, 구분값 연결 결과 |
| `review-input/` | P3. 다음 심의 단계가 사용할 영역별 대표 문구 |
| `preview/` | 원본 페이지 위에 영역을 표시한 확인용 이미지 |

세부 계약은 [출력 계약](docs/출력-계약.md)에 설명되어 있다.

```jsonc
{
  "doc_id": "...",
  "source_file": "광고.pdf",
  "product_group": "예금성",
  "ad_type": "상세페이지",
  "pages": [{
    "page_no": 1,
    "parse_route": "ocr",
    "parse_status": "ok",
    "regions": [{
      "region_id": "p1_r002",
      "bbox": [65, 349, 220, 402],
      "label": "text",
      "role": "본문",
      "lines": [{
        "text": "연이자율",
        "bbox": [65, 349, 220, 402],
        "confidence": 0.998,
        "source": "ocr"
      }]
    }],
    "unassigned_lines": []
  }]
}
```

## 기존 KL Custom Parser 준비 내용

기존에는 제공받은 Custom Parser API 예제의 비동기 호출 방식을 광고 파싱에 적용했다.
현재 저장소에서는 `src/nh_parser/kl_api/`가 당시 보고한 `kl_parser` 역할을 하고,
나머지 `nh_parser` 패키지가 당시 `nh_parsing` 역할을 한다.

```text
호출 측(KL로 가정했으나 등록 가능 여부는 미확인)
  │ POST /ad/parsing: 원본 광고 업로드
  ▼
kl_api
  ├─ 파일 저장
  ├─ 작업 UUID 발급
  ├─ genaikl.status 상태 관리
  └─ 같은 Python 프로세스에서 nh_parser.pipeline.process_file() 호출
       ├─ HTTP → 회사 DGX-Spark PaddleX
       ├─ HTTP → 회사 DGX-Spark Gemma
       └─ 영역 조립·템플릿 선택·구분값 분류
  │
  └─ GET /parsing/result/{uuid}
       └─ kl-core-s2-output.zip 반환
```

광고용 결과 ZIP에는 다음 파일이 들어간다.

- `*_parsed.json`: 모든 문구·좌표·영역과 템플릿 판독 근거를 보존한 P1
- `*_review_input.json`: 템플릿 구분값별 문구와 미배정 문구를 정리한 P3
- `ad_summary.json`: 문서 분류, 적용 템플릿, 페이지·영역·문구 수
- `*_img.zip`: 좌표 박스를 표시한 페이지 확인 이미지가 있을 때 포함

농협 원본 예제에서 확인되는 기본 진입점은 일반 문서용 `POST /parsing`이다. 광고용
`POST /ad/parsing`은 기존 프로젝트에서 추가한 엔드포인트이므로 실제 KL에 등록할 수
있는지는 미팅에서 확인해야 한다. 또한 현재 광고 전용 저장소에는 일반 문서 RAG/HRC
생성기가 없어 `/parsing`은 `501 Not Implemented`를 반환한다.

## 실행 방법

Python 3.13 이상과 `uv`를 기준으로 한다.

```bash
uv sync
cp .env.example .env
```

`.env`에 실행 환경의 모델 주소와 모델명을 설정한다. 실제 내부 주소나 인증값은 커밋하지
않는다.

```dotenv
PADDLEX_URL=http://YOUR_PADDLEX_HOST:8081/layout-parsing
GEMMA_URL=http://YOUR_GEMMA_HOST:4000/v1/chat/completions
GEMMA_MODEL=YOUR_MODEL_NAME
```

파싱 실행:

```bash
uv run python tools/parse.py \
  --input <파일 또는 폴더> \
  --out out/<YYYY-MM-DD>/<run-label> \
  --preview
```

OCR·레이아웃 원본만 빠르게 확인하려면 `--parse-only`를 추가한다. 전체 산출물은 만들되
영역별 VLM 판독을 끄려면 `--region-reading off`를 사용한다. 실행 규칙은
[테스트 실행 지침](docs/테스트-실행-지침.md)을 따른다.

### KL 광고 API 프로토타입 실행

```bash
uv sync --extra kl-api
uv run --extra kl-api python tools/run_kl_parser.py --host 127.0.0.1 --port 9101
```

Swagger UI는 `http://127.0.0.1:9101/docs`, 상태 확인은 `/health`에서 볼 수 있다. 이
서버를 실행했다고 농협 KL에 자동 등록되는 것은 아니다. URL 등록 또는 소스 패키지
업로드 중 어떤 방식을 사용할지는 현장에서 확인해야 한다.

## 폴더 구조

| 경로 | 역할 |
| --- | --- |
| `src/nh_parser/ingest/` | 입력 형식 판별, PDF 렌더링, HWP/HWPX·이미지 적재 |
| `src/nh_parser/ocr/` | 현재 PaddleX 호출, 타일 분할과 OCR 결과 정리 |
| `src/nh_parser/layout/` | 좌표 복원, 중복 제거, 영역·카드·읽기 구조 조립 |
| `src/nh_parser/vlm/` | Gemma 호출, 캐시, 문구·순서·필드 보조 판독 |
| `src/nh_parser/review/` | 템플릿 선택, 구분값 분류, P1/P3 생성 |
| `src/nh_parser/templates/` | 19종 광고 템플릿 카탈로그 |
| `src/nh_parser/kl_api/` | 기존 KL 비동기 규격을 광고 API에 적용한 프로토타입 |
| `src/nh_parser/pipeline.py` | 위 단계를 연결하는 파일 단위 진입점 |
| `tools/parse.py` | 전체 파이프라인 CLI |
| `tools/run_kl_parser.py` | KL 광고 API 프로토타입 실행기 |
| `tools/build_review.py` | 저장된 parse JSON에서 P1/P3 재생성 |
| `tests/` | 외부 모델 없이 검증 가능한 단위·회귀 테스트 |
| `docs/` | 출력 계약, 실행 지침, KL 기존 구현 설명 |

## 테스트

```bash
uv run pytest
```

`tests/`는 배포 런타임에는 필요하지 않지만, 전달받은 소스의 동작과 출력 계약을 검증하는
근거이므로 저장소에 유지한다. 실제 광고 샘플과 실행 산출물은 고객 데이터가 포함될 수
있어 Git에 올리지 않는다.

## HWP/HWPX 입력

HWP/HWPX 입력은 사내 `document-processor`가 필요하다. 사설 저장소이므로 기본
의존성에 넣지 않았고, 설치되지 않은 환경에서도 PDF·이미지 경로는 동작한다.

```bash
uv pip install "document-processor @ git+ssh://git@github.com/CGINSIDE-ROOKIES/document-processor.git"
```

## 현재 범위와 한계

- 규정 검색과 준수 여부 판정은 다음 심의 엔진의 책임이다.
- 현재 PaddleX/Gemma 호출은 회사 개발망 연결이 필요하며 농협 폐쇄망에서는 그대로
  실행할 수 없다.
- 작은 글씨, 특수문자, 표로 검출되지 않은 2열 항목은 현재 모델에서 오류가 발생할 수 있다.
- VLM 결과는 같은 입력에서도 달라질 수 있어 원문 OCR과 판독 근거를 함께 보존한다.
- KL 프로토타입은 프로세스 내부 백그라운드 작업과 파일 상태를 사용하므로 다중 replica용
  운영 큐가 아니다.
- 농협 ETL 연동 소스, 컨테이너 이미지, Custom Parser 등록 파일은 농협과 연동 구조를
  확정한 뒤 준비한다.
