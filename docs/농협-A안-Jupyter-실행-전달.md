# 농협 A안 + KL Custom Parser: Jupyter 실행과 전달물

> 목적: 팀장님 답변 이후 우선 준비할 A안의 실행 순서와 전달물을 확정 가능한 범위까지
> 정의한다. API 가이드, 농협 AWX 원본 예제, 이전 광고 프로토타입, 현재 코드, 구두 회신을
> 서로 다른 근거로 취급한다.

## 1. 현재 이해에 대한 판정

사용자가 정리한 전체 흐름은 맞다.

```text
Knowledge Lake
  -> KL Custom Parser API(현재 kl_api)가 원본 광고 접수
  -> ETLwithLLM 분석 요청 API에 원본 그대로 전달
  -> 상태 및 Default JSON을 ETL API로 조회
  -> nh-ad-parser 광고 파싱/후처리
  -> P1 evidence + P3 review-input + 요약 + 페이지 이미지 생성
  -> KL 호출부가 UUID로 결과 ZIP 조회
  -> 다음 심의 단계에 전달
```

다만 다음 용어와 계약을 구분해야 한다.

1. **KL Custom Parser API**: KL의 HTTP 호출을 받는 비동기 접수·폴링 서버다.
2. **ETLwithLLM**: 농협 DLA/OCR/STR/TSR을 실행하고 Default JSON을 제공한다.
3. **광고 파싱·후처리 로직**: Default JSON을 현재 광고 IR로 바꾸고 영역 조립, 보조
   판독, 템플릿 선택, 구분값 라벨링, P1/P3 출력을 만든다.

따라서 `kl_parser`가 “우리 커스텀 파서를 다시 HTTP로 호출한다”기보다, 같은 서비스
프로세스에서 `run_ad_job()`을 실행하고 그 함수가 ETL HTTP API와 현재 Python 후처리
함수를 차례로 호출한다.

## 2. 근거별로 확정된 것

### ETLwithLLM API 가이드

- 원본 파일과 `tr_data`를 `POST /api/v1/etl/auto/start`에 보낸다.
- 외부 설정 키는 `prj_config.extract_type`이고 `dla`가 기본값이다.
- 상태, 결과 목록, 결과 본문을 각각 1.17~1.19 API로 조회할 수 있다.
- `default` 결과는 DLA 분석 기본 JSON이다.
- `transform()`은 DLA 결과 후처리 훅이지만 실제 농협 설치본에서의 호출·수집 계약은
  확인이 필요하다.

### 농협 AWX Custom Parser 원본 예제

- KL이 `src_file`과 `option`을 multipart로 `POST /parsing`에 보낸다.
- 파서 서버는 HTTP 202와 UUID를 즉시 반환한다.
- KL은 `<최초 URL>/result/{uuid}`를 폴링한다.
- 완료되면 ZIP을 반환한다.
- KL 관리자 화면에서 Custom 파서, 소스코드 제공, Parsing + 구조화 유형으로 프로그램을
  등록하는 절차가 적혀 있다.

### 이전 광고 프로토타입과 현재 이식본

- 원본 규정문서 `/parsing` 외에 광고용 `/ad/parsing`을 추가했다.
- 광고 결과 이름은 `*_parsed.json`, `*_review_input.json`, `ad_summary.json`,
  `*_img.zip`이다.
- 현재 저장소에서는 `NH_KL_AD_BACKEND=etl`일 때 이 앞단과 A안이 연결된다.

### 팀장님 답변과 전달받은 농협 설명

- A안을 우선 준비해도 된다는 방향이다.
- B안 `custom_extension.transform()`은 문서에는 있지만 방문 설명에서 구체적으로 확인한
  방법이 아니므로 현장에서 작동 여부를 확인해야 한다.
- 농협 내부도 vLLM을 사용하므로 기존 VLM 호출 주소를 내부 주소로 바꾸는 방향이다.
- 농협 폐쇄망에는 코드를 반영할 수 있는 Jupyter Notebook 서버를 준비할 예정이라고
  전달받았다.

마지막 두 항목은 API 가이드가 아니라 관계자 회신에 근거한 실행환경 정보다.

## 3. 아직 확정하면 안 되는 것

1. Jupyter 서버가 KL에서 접근 가능한 API 포트를 외부에 열어 주는지
2. Notebook 세션이 종료돼도 Uvicorn/Gunicorn 프로세스를 계속 실행할 수 있는지
3. `/ad/parsing`을 KL Custom Parser의 최초 URL로 등록할 수 있는지
4. KL이 광고 ZIP을 그대로 다음 심의 단계에 전달하는지, 별도 업로드/호출이 필요한지
5. 최종 심의 시스템이 P3 `review-input` 스키마를 승인했는지
6. 내부 vLLM이 현재 코드가 사용하는 멀티모달 입력과 strict `json_schema`
   `response_format`을 지원하는지
7. vLLM 모델명과 인증 헤더가 필요한지
8. Jupyter 서버의 OS, Python 버전, 인터넷/사설 패키지 저장소 접근 여부

팀장님 답변의 “주소만 갈아끼우면 된다”는 것은 OpenAI 호환 vLLM이라는 운영 방향을
뜻한다. 현재 코드는 `/v1/chat/completions`, 이미지 입력, `response_format.type=json_schema`
를 사용하므로 이 세 항목의 실제 호환성은 현장 smoke test가 필요하다.

## 4. Jupyter 서버에서 권장하는 검증 순서

Jupyter는 이번 검증의 **실행 호스트**로 사용할 수 있다. 별도 컨테이너가 필수는 아니다.
다만 KL이 호출할 상시 API로 사용하려면 Jupyter 서버의 네트워크와 프로세스 유지 조건이
충족돼야 한다.

### 1단계 - 소스와 설정 반입

1. Git 저장소 또는 소스 ZIP을 Jupyter 작업 디렉터리에 둔다.
2. `deploy/nh-jupyter.env.example`을 `.env`로 복사한다.
3. 농협 제공 ETL/vLLM 주소, 워크스페이스, 작성자, 필요 시 토큰을 `.env`에 입력한다.
4. 실제 비밀값이 든 `.env`는 반출하거나 Git에 커밋하지 않는다.

### 2단계 - 환경 사전점검

외부 패키지를 하나도 import하지 않는 점검기를 먼저 실행한다.

```bash
python tools/nh_runtime_check.py --mode kl --network \
  --work-dir /농협/허용/작업경로
```

Python 3.13, 필수 패키지, 환경변수, 쓰기 권한, ETL/vLLM TCP 접근을 확인한다. TCP 성공은
API 계약 성공을 의미하지 않는다. 기본적으로 현재 디렉터리의 `.env`를 읽으며 다른 파일은
`--env-file <경로>`로 지정한다.

### 3단계 - ETL API 계약만 확인

```bash
python tools/etl_probe.py run \
  --base-url http://ETL-HOST:PORT \
  --ws-id PROVIDED_WS_ID \
  --author PROVIDED_AUTHOR \
  --input sample.pdf
python tools/etl_probe.py summary
```

이 단계는 `nh_parser` 패키지나 VLM 없이 수행된다. 원본 요청/응답은 `probe-out/`에만
남고 Git에는 올라가지 않는다. `extract_type` 기본값을 그대로 쓸 수 있지만 명시한다면
`--prj-config '{"extract_type":"dla"}'`를 사용한다.

### 4단계 - 저장된 Default JSON으로 후처리 경계 확인

현장 응답의 wrapper, 페이지 수, bbox, 표 구조가 현재 변환기에 맞는지 테스트한다.
이 단계에서 PaddleX 호출이 발생하면 구현 오류다.

### 5단계 - 내부 vLLM 호환 확인

다음 항목을 작은 광고 1건으로 확인한다.

- `/v1/chat/completions` 주소
- 정확한 vision 모델명
- 이미지가 포함된 `messages[].content`
- strict JSON schema 구조화 응답
- timeout과 최대 토큰
- 인증 필요 여부

고객 문서 대신 도구에 내장된 흰색 PNG로 요청 계약만 먼저 확인한다.

```bash
python tools/vllm_probe.py
```

주소만 변경해 성공하면 현재 `GEMMA_URL`, `GEMMA_MODEL` 설정으로 충분하다. 실패하면
응답 원문을 확보한 뒤 vLLM 어댑터 범위를 결정한다.

### 6단계 - KL 없이 A안 한 건 완주

```bash
uv run python tools/etl_review_run.py \
  --base-url http://ETL-HOST:PORT \
  --ws-id PROVIDED_WS_ID \
  --author PROVIDED_AUTHOR \
  --input sample.pdf \
  --out out/YYYY-MM-DD/nh-etl-a-smoke
```

`parse`, `evidence`, `review-input`, `pages`를 확인한다. 현장 Python 환경에서 `uv`를 쓸 수
없다면 설치된 Python으로 같은 도구를 실행한다.

### 7단계 - KL Custom Parser API 왕복

```bash
uv run --extra kl-api python tools/run_kl_parser.py \
  --host 0.0.0.0 --port PROVIDED_PORT
```

1. `/health`가 `ad_backend=etl`을 반환하는지 확인한다.
2. `/ad/parsing`으로 테스트 파일을 보내 UUID를 받는다.
3. `/parsing/result/{uuid}`를 폴링해 ZIP을 받는다.
4. 이 로컬 왕복이 성공한 뒤 KL 관리자 등록과 실제 KL 호출을 시험한다.

Jupyter 셀에서 서버를 직접 띄우면 셀 중단이나 커널 재시작에 함께 종료될 수 있다. 초기
검증에는 가능하지만, KL 등록용 상시 서비스는 농협이 제공하는 터미널/background job,
프로세스 관리자 또는 별도 컨테이너 방식 중 하나가 필요하다.

## 5. 전달해야 할 작업물

### 지금 전달 가능한 방향 검토본

| 작업물 | 형태 | 포함 내용 |
| --- | --- | --- |
| 소스 | Git 브랜치/커밋 또는 `git archive` ZIP | `src/`, `tools/`, `integrations/`, 테스트 |
| 실행 설정 예시 | `deploy/nh-jupyter.env.example` | 실제 비밀값 없는 ETL/vLLM/A안 설정 |
| KL API | `src/nh_parser/kl_api/` | 접수, UUID, 상태 폴링, ZIP 반환 |
| A안 클라이언트 | `src/nh_parser/integrations/etlwithllm/` | ETL 요청·폴링·Default JSON·후처리 경계 |
| 현장 점검기 | `tools/etl_probe.py`, `tools/nh_runtime_check.py`, `tools/vllm_probe.py` | 무의존성 ETL·vLLM 계약 및 환경 점검 |
| 설명서 | `docs/농협-A안-Jupyter-실행-전달.md` 등 | 흐름, 실행 순서, 미확정 계약 |
| 검증 | pytest 결과 | 목 응답과 로컬 API 규격 회귀 테스트 |

### 환경 정보를 받은 뒤 만들어야 할 실제 반입본

| 작업물 | 왜 지금 확정할 수 없는가 |
| --- | --- |
| 오프라인 wheel 묶음 | 농협 Jupyter의 OS/CPU/Python 버전이 필요 |
| 설치 스크립트 | 인터넷 또는 내부 PyPI 사용 가능 여부가 필요 |
| 상시 실행 스크립트/서비스 | Jupyter의 프로세스 관리자와 포트 노출 방식이 필요 |
| 컨테이너 이미지 | 농협이 컨테이너 반입·실행을 요구하는지 미확정 |
| 최종 API 산출물 계약 | KL/심의 시스템이 요구하는 광고 결과 스키마 확인 필요 |
| 농협 vLLM 어댑터 | 주소 교체만으로 실제 멀티모달/JSON schema가 되는지 확인 필요 |

따라서 지금 Git 소스만 전달하고 “완성 배포본”이라고 하면 안 된다. 현재 전달물은
**A안 실행 가능한 방향 검토본 + 현장 계약 점검 도구**이며, 현장에서 환경 정보를 확보한
뒤 같은 커밋을 기준으로 오프라인 설치/상시 실행 묶음을 만드는 순서가 맞다.

## 6. 농협/팀장님께 추가로 받을 값

1. Jupyter OS와 `python --version`
2. 터미널 사용 및 background 프로세스 실행 가능 여부
3. KL에서 접근 가능한 Jupyter 서버 주소·포트 제공 여부
4. 패키지 설치 방식: 인터넷, 내부 PyPI, 사전 반입 wheel 중 무엇인지
5. ETL base URL, `ws_id`, `author`, 인증 사용 여부
6. vLLM chat completions URL, vision 모델명, 인증 여부
7. `/ad/parsing` Custom Parser 등록 가능 여부
8. 광고 결과 ZIP/P1/P3의 최종 수신 주체와 승인 스키마
