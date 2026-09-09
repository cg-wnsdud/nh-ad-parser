# `kl_parser` 선택적 API - 현재 저장소 이식본

## 결론

`kl_parser`는 OCR/DLA 또는 RAG 엔진이 아니다. Knowledge Lake/AWX가 요구하는 비동기
HTTP 통신을 실제 광고 파이프라인 앞에 붙이는 **선택적 어댑터**다.

현재 저장소는 광고 파싱 전용이므로 다음만 구현한다.

- `POST /ad/parsing`: 광고 파일 접수 및 작업 UUID 반환
- `GET /parsing/result/{uuid}`: 작업 상태 또는 `kl-core-s2-output.zip` 반환
- `GET /health`: API 프로세스와 선택한 백엔드 설정 확인
- `POST /parsing`: 일반 문서 RAG/KL exporter가 없음을 명확히 알리는 `501`

Knowledge Lake가 이 통신 규격을 요구하지 않으면 이 서버는 실행할 필요가 없다.

## 기존 보고와 현재 구조의 차이

이전 `nh-ad-review-poc/kl_parser`에는 두 트랙이 같이 있었다.

| 트랙 | 실제 엔진 | 목적 |
| --- | --- | --- |
| `/parsing` | `nh_parsing.rag_ingest` + `kl_export` | 일반 문서를 청크/이미지로 바꾸어 KL 색인·RAG에 적재 |
| `/ad/parsing` | `nh_parsing.ad_export` | 광고 원문·좌표·템플릿 연결 결과를 심의 단계로 전달 |

따라서 예전 프로젝트 전체에는 RAG용 파이프라인이 있었지만, 광고 파이프라인까지 RAG를
목적으로 만든 것은 아니다. 현재 `nh-ad-parser`는 광고물에서 문구·좌표·영역·템플릿
근거를 만드는 부분만 이관한 저장소다. `rag_ingest`, `kl_export`는 이관 범위에 없다.

## 가능한 실행 구조

### Knowledge Lake가 호출하지 않는 경우

`kl_parser`가 필요 없다.

```text
별도 실행기(A안) -> ETLwithLLM HTTP API -> 농협 DLA JSON -> nh-ad-parser 후처리
```

또는:

```text
ETLwithLLM -> DLA -> custom_extension.transform()(B안) -> nh-ad-parser 후처리
```

### Knowledge Lake가 AWX 규격으로 호출하는 경우

이때 `kl_parser`가 최초 진입점이다.

```text
Knowledge Lake
  -> POST /ad/parsing (`kl_parser`)
  -> ETLwithLLM HTTP API (A안)
  -> nh-ad-parser 후처리
  -> GET /parsing/result/{uuid}
  -> kl-core-s2-output.zip
```

B안은 ETLwithLLM이 후처리 전체를 소유하므로, 그 결과를 Knowledge Lake에 누가 어떤
API로 돌려줄지 별도 계약이 필요하다. 단순히 `transform()`을 구현한다고 기존 AWX
`/ad/parsing` 계약이 자동으로 생기지는 않는다.

## 설치 및 실행

FastAPI 관련 패키지는 광고 파서 기본 의존성이 아니라 `kl-api` extra로 분리했다.

```bash
uv sync --extra kl-api
uv run --extra kl-api python tools/run_kl_parser.py --host 127.0.0.1 --port 9101
```

Swagger는 `http://127.0.0.1:9101/docs`, 상태 확인은 다음과 같다.

```bash
curl http://127.0.0.1:9101/health
```

새 파싱 실행은 저장소 테스트 규칙에 맞춰 기본적으로
`out/<YYYY-MM-DD>/kl-parser-runtime/` 아래에 작업 폴더를 만든다. 농협 배포에서는
`PATH_TEMP`로 AWX가 요구하는 마운트 경로를 지정할 수 있다.

## 백엔드 선택

### `local` - 기존 개발 환경

```text
NH_KL_AD_BACKEND=local
```

현재 `nh_parser.pipeline.process_file()`을 호출한다. `PADDLEX_URL`, `GEMMA_URL`,
`GEMMA_MODEL`이 필요하며 DGX 개발 회귀 비교용이다.

### `etl` - A안과 결합

```text
NH_KL_AD_BACKEND=etl
ETL_BASE_URL=http://YOUR_ETLWITHLLM_HOST
ETL_WS_ID=YOUR_WORKSPACE_ID
ETL_AUTHOR=YOUR_AUTHOR
ETL_PROJECT_CONFIG={"extractType":"dla"}
```

ETLwithLLM 공개 HTTP API로 원본 문서를 제출하고 Default JSON을 받아 현재 광고
후처리에 연결한다. `ETL_AUTHORIZATION`은 농협 제공 인증을 헤더로 전달해야 하는
배포에서만 설정한다. 로그인·워크스페이스 생성 자체는 구현하지 않는다.

주의: 이 경로는 PaddleX OCR/DLA만 ETLwithLLM 결과로 대체한다. 현재 파이프라인의
문서 분류, 영역 보조 판독, 템플릿 선택·라벨링에는 Gemma 호출 경로가 남아 있다.
농협 폐쇄망에서 완전 실행하려면 다음 중 하나를 별도로 결정해야 한다.

1. `GEMMA_URL`을 농협이 제공하는 OpenAI 호환 LLM/VLM 엔드포인트로 교체
2. ETLwithLLM이 제공하는 LLM 기능에 맞는 어댑터 구현
3. VLM 단계를 비활성화하고 미확정 값을 검수 대상으로 남기는 제한 모드 구현

## 반환 ZIP

| 파일 | 현재 저장소의 원천 |
| --- | --- |
| `*_parsed.json` | `evidence(P1)`: 전체 문구·좌표·파서/VLM 근거·템플릿 연결 |
| `*_review_input.json` | `review-input(P3)`: 다음 심의 단계용 영역별 선택 문구 |
| `ad_summary.json` | 분류, 템플릿, 페이지/영역/문구 수, 사용 백엔드 |
| `*_img.zip` | 원본 대조용 페이지 미리보기 이미지 |

이 이름은 이전 보고와의 연결을 유지하기 위한 API 외부 이름이다. 저장소 내부 정식 명칭은
각각 P1 `evidence`와 P3 `review-input`이다.

## 현재 한계

- 농협 실서버, 인증, 워크스페이스, 실제 Default JSON으로 검증하지 않았다.
- 백그라운드 작업은 FastAPI 프로세스 안에서 실행되며 영속 큐가 아니다.
- 상태는 로컬/마운트 파일시스템에 저장된다. 다중 replica 운영에는 공유 스토리지나
  별도 작업 상태 저장소가 필요하다.
- `/parsing` RAG 트랙은 구현하지 않았다. 광고 과제 범위와 별도로 요구가 확정될 때만
  기존 `rag_ingest`/`kl_export` 이식을 검토한다.
- B안 `transform()`과 AWX 결과 조회 API의 연결은 가이드에 정의돼 있지 않다.
