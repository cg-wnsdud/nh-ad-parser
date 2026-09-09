# A안·KL 전체 흐름과 농협 폐쇄망 실행 조건

> 상태: 팀장 및 농협 담당자에게 구조를 확인받기 위한 구현 설명서다. 농협 실서버에서
> 검증된 운영 설계가 아니다. 문서 근거, 원본 코드 근거, 현재 구현, 미확정을 구분한다.

## 1. 한 문장 결론

A안은 **농협 폐쇄망 안에서 별도로 실행되는 `nh-ad-parser`가 ETLwithLLM HTTP API에
원본 파일을 보내고, 농협 DLA의 Default JSON을 받아 광고 심의용 P1/P3 JSON으로
후처리하는 구조**다.

`kl_parser`는 이 처리의 필수 엔진이 아니다. 상위 시스템이 농협 AWX Custom Parser
규격으로 파일을 넣고 UUID로 결과를 폴링해야 할 때만 앞단에 붙는 선택적 HTTP 어댑터다.

## 2. 근거의 범위

| 구분 | 확인된 내용 | 아직 확인되지 않은 내용 |
| --- | --- | --- |
| ETLwithLLM API 가이드 | `/api/v1/etl/auto/start`, `file/info`, `result/list`, `result/doc`; `prj_config.extract_type`; Default JSON | 농협 설치본의 실제 인증 설정, 주소, 지원 파일, 운영 제한 |
| 농협 AWX 원본 예제 | `POST /parsing` 접수, UUID 즉시 반환, `GET /parsing/result/{uuid}` 폴링, 결과 ZIP | 광고 전용 `/ad/parsing` |
| 이전 광고 프로토타입 | `/ad/parsing`을 추가해 광고 JSON/ZIP 반환 | 이 추가 URL을 실제 Knowledge Lake에 등록할 수 있는지 |
| 현재 저장소 | A안 ETL 클라이언트, DLA 변환, P1/P3 후처리, 선택적 `kl_api` | 농협 실서버·실제 Default JSON·농협 VLM과의 왕복 |

따라서 아래 문장에서 “KL이 호출한다”는 것은 **KL에 이 Custom Parser 서버 주소와
엔드포인트가 등록되어 KL이 HTTP 요청을 보내도록 구성됐을 때**라는 뜻이다. 같은 망에
서버를 띄운 것만으로 KL이 자동 발견하거나 호출하지 않는다.

## 3. A안의 두 가지 진입 형태

### 3.1 KL 없이 A안을 직접 실행

상위 배치나 담당자가 우리 실행기를 직접 실행한다면 `kl_parser`는 필요 없다.

```text
담당자 또는 농협 내부 배치
  └─ tools/etl_review_run.py
       ├─ 원본 PDF/PNG/JPG
       ├─ ETLwithLLM 분석 API 호출
       ├─ 농협 DLA Default JSON 수신
       └─ nh-ad-parser 후처리
            ├─ parse/<문서>.json
            ├─ evidence/<문서>.json       (P1)
            ├─ review-input/<문서>.json   (P3)
            └─ pages/                     (미리보기)
```

이 형태는 “파일 하나를 넣어 결과 디렉터리를 만든다”는 실행 방식이다. 상위 시스템에
UUID API를 제공하지 않는다.

### 3.2 KL이 앞단에서 호출하는 A안

KL/AWX 호출 계약까지 필요하다고 확정되면 선택적 FastAPI 서버를 앞에 둔다.

```text
Knowledge Lake 또는 AWX 호출부
  │
  │ 1) multipart 파일 요청
  ▼
kl_parser FastAPI (`src/nh_parser/kl_api/`)
  │ 2) UUID 즉시 반환, 백그라운드 작업 시작
  │
  │ 3) NH_KL_AD_BACKEND=etl
  ▼
ETLwithLLM 공개 HTTP API
  │ 4) 농협 DLA/STR/TSR 수행
  │ 5) Default JSON 반환
  ▼
nh-ad-parser 공통 후처리
  │ 6) P1/P3/요약/페이지 이미지 생성
  ▼
kl_parser 작업 폴더
  │
  │ 7) 호출부가 UUID로 상태 폴링
  ▼
kl-core-s2-output.zip 반환
```

여기서 엔드포인트는 아직 결정이 필요하다.

- 농협 제공 AWX 원본 예제에서 확정된 접수 URL: `POST /parsing`
- 이전 광고 프로토타입과 현재 이식본이 추가한 URL: `POST /ad/parsing`
- 현재 이식본의 `POST /parsing`: 일반 RAG exporter가 없음을 알리는 `501`

즉 실제 KL 연결 전에 **광고용 URL을 `/ad/parsing`으로 별도 등록할 수 있는지**, 아니면
광고 전용 배포에서는 원본 규격대로 `/parsing`이 광고 작업을 받도록 해야 하는지 확인해야
한다. 이 답 없이 “KL 연동 완료”라고 보고하면 안 된다.

## 4. 현재 코드에서 요청 한 건이 흐르는 순서

### 4.1 `kl_parser` 접수·폴링 계층

1. `src/nh_parser/kl_api/app.py:create_app()`이 FastAPI 앱을 만든다.
2. `POST /ad/parsing`이 multipart의 `src_file`과 선택적 `option`을 받는다.
3. 32자 UUID와 `PATH_TEMP/<uuid>/image` 작업 폴더를 만들고 원본을 저장한다.
4. `genaikl.status`에 `PARSING`을 기록한다.
5. FastAPI `BackgroundTasks`에 `run_ad_job()`을 등록한다.
6. 파싱 완료를 기다리지 않고 HTTP 202와 `{uuid, timeout}`을 즉시 반환한다.
7. 호출부가 `GET /parsing/result/{uuid}`를 반복 호출한다.
8. 상태가 `PARSING`이면 상태 JSON, `ERROR`면 오류 메시지, `DONE`이면 결과 ZIP을
   반환한다.

이 구조는 농협 AWX 원본 예제의 UUID·`genaikl.status`·폴링 방식을 이식한 것이다.
다만 원본은 작업을 별도 subprocess로 실행해 timeout 시 종료하는 반면, 현재 이식본은
FastAPI 프로세스의 `BackgroundTasks`를 사용한다. 따라서 현재 구현의 `TIMEOUT`은 응답에
알리는 대기 시간 값일 뿐, 실행 중인 작업을 강제 종료하지 않는다. 운영 전 보완 항목이다.

### 4.2 A안 ETL 호출 계층

`src/nh_parser/kl_api/service.py:run_ad_job()`은 `NH_KL_AD_BACKEND`를 읽는다.

- `local`: 기존 `process_file()`을 호출하므로 개발용 PaddleX/Gemma가 필요하다.
- `etl`: `_process_etl()`을 호출하므로 농협 ETLwithLLM DLA 결과를 사용한다.

`etl`일 때 정확한 호출 순서는 다음과 같다.

1. `ETL_BASE_URL`, `ETL_WS_ID`, `ETL_AUTHOR`를 읽는다.
2. `ETL_PROJECT_CONFIG`를 읽는다. 기본은 `{"extract_type":"dla"}`다.
3. 농협에서 받은 로그인 토큰이 있으면 `ETL_TOKEN`을 `token` 쿠키로 설정한다.
4. `POST /api/v1/etl/auto/start`에 다음 multipart를 보낸다.

   ```text
   tr_data = JSON 문자열
   upfiles = 원본 파일
   ```

   ```json
   {
     "author": "<농협 제공/확인 값>",
     "ws_id": "<농협 제공 workspace id>",
     "res_type": "default",
     "prj_config": {"extract_type": "dla"},
     "meta_info": {}
   }
   ```

5. 응답의 `file_paths[0]`을 보관한다. 이후 조회 키는 `task_id`가 아니라 이
   `file_path`다.
6. `GET /api/v1/file/info?file_path=...`를 호출해 `chunk_status`가 `002`가 될 때까지
   폴링한다. `999`면 실패 처리한다.
7. `GET /api/v1/file/result/list?docPath=...`로 결과 목록을 받는다.
8. 목록에서 원본명과 대응하는 Default JSON을 고른다.
9. `GET /api/v1/file/result/doc?docResultPath=...`로 JSON 내용을 받는다.
10. 문서에 나타난 래퍼 차이(`data`, `doc_result`, `doc_result.default`)를 정규화한다.

로그인과 워크스페이스 생성은 구현하지 않는다. 가이드에 따르면 로그인을 쓰는 환경의
토큰은 `Set-Cookie: token=...`으로 오며, 여기서는 농협이 제공한 토큰 값만 사용한다.
`ETL_AUTHORIZATION`은 별도 게이트웨이가 요구한다고 확인된 경우에만 쓰는 보조 설정이며
가이드의 기본 인증 방식으로 단정하지 않는다.

### 4.3 농협 DLA 결과 이후의 광고 후처리

`src/nh_parser/integrations/etlwithllm/pipeline.py:process_file_from_dla()`가 A안과 B안의
공통 경계다.

```text
ETL Default JSON
  ├─ pages/paragraphs/lines/table 구조 읽기
  ├─ ETL 페이지 크기 → 우리 렌더 이미지 크기로 bbox 변환
  ├─ 기존 Region/Line 구조로 조립
  ├─ 페이지별 보조 VLM 판독·카드/읽기순서 판단
  ├─ 문서 종류 분류
  ├─ 19종 광고 템플릿 선택
  ├─ 회사명·상품명·금리·유의사항 등 구분값 라벨링
  └─ P1 evidence와 P3 review-input 생성
```

이 경로는 기존 PaddleX 호출과 타일 DLA를 실행하지 않는다. 농협 DLA가 그 역할을 이미
수행했기 때문이다. 그러나 **VLM 단계까지 없어지는 것은 아니다.** 현재 코드에는 영역
보조 판독, 카드 판단, 문서 분류, 템플릿 선택·라벨링에서 Gemma 호출 경로가 남아 있다.
따라서 농협 폐쇄망에서 완주하려면 농협 VLM의 호출 주소·인증·모델명·요청 형식이 현재
OpenAI 호환 클라이언트와 맞는지 확인하거나 별도 어댑터/제한 모드를 구현해야 한다.

현재 DLA 결합 경로의 입력 지원은 PDF/PNG/JPG다. HWP/HWPX를 ETL이 처리할 수 있다는
사실과, 현재 우리 후처리 코드가 원본을 다시 렌더할 수 있다는 것은 별개다. HWP/HWPX
지원은 아직 구현 완료로 볼 수 없다.

## 5. `extract_type`은 어디에 적용되는가

정확한 외부 키는 p.29의 `prj_config.extract_type`이다. 현재 A안은
`ETL_PROJECT_CONFIG` 또는 CLI `--project-config`로 이 값을 ETL 요청에 넣는다.

| 값 | 가이드 설명 | 현재 A안에서의 판단 |
| --- | --- | --- |
| `dla` | DLA 분석 | 광고 PDF/이미지의 기준값. 농협 레이아웃·OCR 결과를 받기 위해 사용 |
| `all` | DLA + 직접 파싱 | 결과 병합 규칙을 실서버에서 확인하기 전에는 기준값으로 쓰지 않음 |
| `parser` | HWPX·DOCX 직접 파싱 | 현재 PDF/이미지 DLA 후처리 경로의 대체값이 아님 |

`res_type=default`는 “어떤 결과 파일을 받을지”이고, `extract_type=dla`는 “어떤 분석
경로를 수행할지”다. 서로 다른 설정이다.

가이드 3장의 내부 enum `ExtractType.CUSTOMIZE`와 외부 API의 문자열 매핑은 문서에 없다.
따라서 현재 공개 API 클라이언트는 `customize`를 보내지 않는다.

## 6. 농협 폐쇄망에서 실제로 실행한다는 의미

A안은 ETL 컨테이너에 코드를 마운트하는 B안과 다르다. 우리 프로그램은 농협 폐쇄망의
별도 실행 위치에서 돌아가며, 그 위치에서 ETL API 주소로 통신할 수 있어야 한다.

가능한 배포 단위는 소스+오프라인 wheel 묶음 또는 우리 서비스용 컨테이너 이미지다.
어느 쪽인지는 현재 자료로 확정할 수 없다. 필요한 조건은 같다.

1. 농협 내부 Linux/Windows 서버 또는 컨테이너 실행 환경
2. Python 버전 호환성: 현재 패키지는 Python 3.13 이상을 선언함
3. 인터넷 없이 설치 가능한 Python wheel과 필요한 시스템 라이브러리
4. 원본·상태·결과를 쓸 작업 디렉터리
5. 이 프로세스에서 ETLwithLLM과 농협 VLM으로 나가는 내부 네트워크 경로
6. KL이 호출한다면 KL에서 이 FastAPI의 서비스 주소/포트로 들어오는 경로
7. `ETL_BASE_URL`, `ETL_WS_ID`, `ETL_AUTHOR`, 필요 시 `ETL_TOKEN`

개발 PC에서 쓰는 `uv sync`는 인터넷/패키지 저장소가 없는 폐쇄망 배포 방법이 아니다.
현장에는 의존성 없는 `tools/etl_probe.py`로 API 계약부터 확인하고, 운영 반입 방식은
농협이 허용하는 소스 반입/이미지 반입 정책을 받은 뒤 결정해야 한다.

KL 없이 A안을 직접 검토하는 명령 예시는 다음과 같다.

```bash
uv run python tools/etl_review_run.py \
  --base-url http://ETL-HOST:PORT \
  --ws-id PROVIDED_WS_ID \
  --author PROVIDED_AUTHOR \
  --input sample.pdf \
  --out out/2026-09-09/etl-external-review
```

KL 앞단까지 검토할 때는 환경을 설정하고 별도 API 프로세스를 띄운다.

```text
NH_KL_AD_BACKEND=etl
ETL_BASE_URL=http://ETL-HOST:PORT
ETL_WS_ID=PROVIDED_WS_ID
ETL_AUTHOR=PROVIDED_AUTHOR
ETL_PROJECT_CONFIG={"extract_type":"dla"}
ETL_TOKEN=<인증이 켜진 경우 받은 token 값>
PATH_TEMP=<농협이 허용한 작업 경로>
```

```bash
uv run --extra kl-api python tools/run_kl_parser.py --host 0.0.0.0 --port 9101
```

`127.0.0.1`은 같은 서버 안에서만 접근 가능하다. KL이 다른 서버에서 호출한다면
`0.0.0.0` 또는 지정 인터페이스에 바인딩하고 방화벽·서비스 등록을 농협 운영 기준에
맞춰야 한다. 이것은 예시일 뿐, 실제 포트와 노출 방식은 농협이 정해야 한다.

## 7. 지금 확정할 수 없는 것

아래는 코드만 보고 답할 수 없으며 농협/AgileSoDA/팀장 확인이 필요하다.

1. 최상위 호출자가 KL인지, 별도 배치인지, 사람이 실행하는 도구인지
2. KL 광고 파서 접수 URL을 `/ad/parsing`으로 등록할 수 있는지
3. 아니라면 광고 전용 배포의 `/parsing`을 광고 처리로 바꿔도 되는지
4. KL이 기대하는 최종 산출물이 기존 `_hrc.jsonl/_hrc.json`인지, 광고 P1/P3 ZIP인지
5. ETL 인증 미들웨어 사용 여부와 토큰 전달·갱신 방법
6. 실제 `author` 값의 의미와 필수 여부
7. 농협 내부 VLM 주소·인증·모델·OpenAI 호환 여부
8. 별도 서비스/컨테이너 실행 허용 여부와 Python 버전
9. 파일 크기·확장자·동시 처리·폴링 주기 제한
10. 다중 인스턴스 또는 재시작을 견디는 영속 작업 큐가 필요한지

## 8. 팀장님께 먼저 확인할 질문

> A안 기준으로는 농협 폐쇄망 안의 별도 `nh-ad-parser` 프로세스가 원본을
> ETLwithLLM에 `prj_config.extract_type=dla`, `res_type=default`로 제출하고,
> Default JSON을 받아 광고 P1/P3 후처리를 수행하도록 준비했습니다.
>
> 다만 농협 제공 AWX 원본 예제의 공식 접수 URL은 `/parsing`이고, 광고용
> `/ad/parsing`은 이전 광고 프로토타입에서 추가한 URL입니다. 이번 범위에서 Knowledge
> Lake가 직접 호출하는 구조가 맞는지, 맞다면 광고용 `/ad/parsing`을 별도 등록할 수
> 있는지 확인 부탁드립니다. 별도 등록이 안 된다면 광고 전용 배포에서 `/parsing`이
> 광고 작업을 받도록 맞춰야 합니다.
>
> 또한 A안의 OCR/DLA는 농협 ETL 결과로 대체되지만 현재 후처리에는 VLM 호출이 남아
> 있습니다. 농협 내부 VLM의 주소·인증·모델명·요청 규격과, 별도 서비스 또는 컨테이너
> 실행이 가능한 환경/Python 버전도 함께 확인 부탁드립니다.

