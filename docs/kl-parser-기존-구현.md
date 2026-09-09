# KL Custom Parser 광고 API 기존 구현

## 이 문서의 목적

농협에서 제공한 `1.awx_custom_parser_example_api`를 참고해 이전에 구현·보고한 광고
파싱 API가 무엇이었는지, 현재 저장소에서는 어느 코드에 해당하는지 설명한다. 이 구현은
농협 운영 환경에서 등록까지 완료된 확정본이 아니라 **미팅에서 연동 구조를 확인하기 위한
동작 가능한 프로토타입**이다.

## 이전 보고 내용과 현재 코드의 대응

| 이전 보고 용어 | 현재 저장소 | 역할 |
| --- | --- | --- |
| `kl_parser` | `src/nh_parser/kl_api/` | 파일 접수, UUID, 상태 조회, ZIP 응답 |
| `nh_parsing` | `src/nh_parser/` | 실제 광고 파싱·영역 조립·VLM·템플릿 처리 |
| 광고 API 실행 | `tools/run_kl_parser.py` | Uvicorn으로 FastAPI 앱 실행 |
| `*_parsed.json` | P1 `evidence` | 모든 문구·좌표·판독·템플릿 근거 |
| `*_review_input.json` | P3 `review-input` | 다음 심의 단계에 전달할 구조화 문구 |

이전 `nh-ad-review-poc`에서 광고 파싱 계층을 별도 저장소로 옮기면서 패키지 이름과 파일
배치가 달라졌지만, KL 통신 계층이 파싱 함수를 같은 프로세스에서 호출한다는 구조는 같다.

## 구현한 비동기 흐름

```text
1. 호출 측 → POST /ad/parsing
   multipart/form-data: src_file, option(선택)

2. kl_api
   원본 저장 → UUID 작업 폴더 생성 → genaikl.status=PARSING
   → 백그라운드에서 nh_parser.pipeline.process_file() 실행

3. 호출 측 → GET /parsing/result/{uuid}
   PARSING: {"status":"PARSING"}
   ERROR:   {"status":"ERROR","message":"..."}
   DONE:    kl-core-s2-output.zip
```

접수 성공 응답 예시는 다음과 같다.

```json
{
  "result": "OK",
  "body": {
    "uuid": "32자리-hex-작업-id",
    "timeout": 1800
  }
}
```

코드 진입점은 다음과 같다.

```text
src/nh_parser/kl_api/app.py
  request_ad_parsing()
    → BackgroundTasks.add_task(run_ad_job, ...)

src/nh_parser/kl_api/service.py
  run_ad_job()
    → nh_parser.pipeline.process_file()
    → resolve_template() / label_regions()
    → build_evidence() / build_review_input()
```

## 현재 모델 호출 흐름

`kl_api`가 다른 파서 API를 HTTP로 다시 호출하는 구조가 아니다. 같은 Python 프로세스에서
현재 파이프라인의 `process_file()` 함수를 직접 호출한다. 원격 HTTP 요청은 실제 모델
추론 단계에서 발생한다.

```text
호출 측
  → kl_api FastAPI
      → Python 함수 호출: nh_parser.pipeline.process_file()
          → HTTP: DGX-Spark PaddleX PP-StructureV3
          → HTTP: DGX-Spark Gemma VLM
          → Python 후처리: 영역·읽기 순서·템플릿·구분값
      → 결과 ZIP
```

따라서 “`kl_parser`가 우리 커스텀 파서를 호출한다”는 표현은 개념적으로는 맞지만,
기술적으로는 **별도 서버 간 호출이 아니라 API 계층이 같은 저장소의 파싱 함수를 호출한다**가
정확하다.

## 결과 ZIP

`kl-core-s2-output.zip`에는 다음 파일만 넣는다.

| 파일 | 내용 |
| --- | --- |
| `<원본명>_parsed.json` | 모든 문구, 좌표, 영역, VLM/Judge 근거, 템플릿 연결 결과 |
| `<원본명>_review_input.json` | 템플릿 구분값별 선택 문구와 미배정 문구 |
| `ad_summary.json` | 분류, 적용 템플릿, 페이지·영역·문구 수, 실행 백엔드 |
| `<원본명>_img.zip` | 페이지 확인 이미지가 생성된 경우 |

현재 파이프라인의 `--preview` 계층을 재사용하므로 이미지에는 원본 위에 문구·영역 좌표
박스가 표시된다. 이전 보고의 “원본 확인용 페이지 이미지”는 **원본 대조를 위한 검수용
이미지**라는 뜻이며, 아무 표시가 없는 원본 렌더와는 다르다.

## 농협 원본 예제와 다른 부분

농협 예제가 직접 정의하는 것은 일반 문서용 `POST /parsing`과 HRC 결과다. 광고용
`POST /ad/parsing`과 P1/P3 파일은 기존 프로젝트에서 추가한 확장이다.

| 구분 | 농협 제공 예제 | 현재 광고 프로토타입 |
| --- | --- | --- |
| 접수 API | `POST /parsing` | `POST /ad/parsing` |
| 결과 조회 | `GET /parsing/result/{uuid}` | 같은 경로 재사용 |
| 결과 | `_hrc.jsonl`, `_hrc.json`, `_img.zip` | `_parsed.json`, `_review_input.json`, `ad_summary.json`, `_img.zip` |
| 목적 | 일반 문서 KL 색인·RAG | 광고 문구·근거를 다음 심의 단계로 전달 |

현재 저장소에는 `rag_ingest`와 `kl_export`가 없으므로 일반 문서 `/parsing`은 의도적으로
`501 Not Implemented`를 반환한다. 광고용 URL과 결과 ZIP을 KL에 등록할 수 있는지는
농협 측 확인이 필요하다.

## ETLwithLLM을 받기 전의 전제

기존 구현은 농협 내부에서도 PaddleX와 Gemma에 해당하는 모델 서비스 주소만 교체하면
된다는 가정을 두었다. 새로 받은 ETLwithLLM 가이드를 보면 OCR/DLA 쪽은 단순한 모델
엔드포인트 교체가 아니라 원본 파일 단위 비동기 분석 API일 가능성이 높다.

현재 방식:

```text
process_file()
  → 페이지 렌더·타일 분할
  → 타일마다 PaddleX 동기 호출
  → 즉시 OCR/레이아웃 응답
```

ETLwithLLM API 방식으로 예상되는 흐름:

```text
원본 파일 제출
  → 분석 작업 접수
  → 상태 폴링
  → 문서 전체 DLA/OCR 결과 조회
  → 결과를 우리 AdDocument로 변환
  → 기존 후처리
```

따라서 `extract_type`, threshold 같은 파라미터 조정은 필요하지만 그것만으로 연동이
끝나지는 않는다. 파일 단위 작업 관리, 결과 JSON 변환, 좌표 정합성, 표·confidence 차이,
농협 내부 VLM 호환을 함께 처리해야 한다.

## 실행

```bash
uv sync --extra kl-api
uv run --extra kl-api python tools/run_kl_parser.py --host 127.0.0.1 --port 9101
```

상태 확인:

```bash
curl http://127.0.0.1:9101/health
```

이 실행은 현재 DGX-Spark 모델 주소를 요구한다. 농협 폐쇄망에서 바로 실행할 수 있는
배포본이라는 의미는 아니다.
