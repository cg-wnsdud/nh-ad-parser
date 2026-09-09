# ETLwithLLM 연동 후보 - 팀장 검토용

> 상태: **방향 결정용 프로토타입**. 아직 농협 폐쇄망 반입본이나 운영 배포본이 아니다.

이 폴더는 “농협 OCR을 쓰면서 현재 광고 파싱 파이프라인을 어디에 연결할 것인가”를
코드로 비교하기 위한 자료다. 가이드에 명시된 부분은 구현했고, 명시되지 않은 동작은
실행 가능한 것처럼 추정하지 않았다.

## 1. 먼저 알아야 할 세 시스템

이름에 `parser`와 `API`가 반복되지만 서로 다른 시스템이다.

```text
Knowledge Lake(KL)       문서 적재·색인 시스템
ETLwithLLM               농협 DLA/STR/TSR 분석 플랫폼
nh-ad-parser             씨지인사이드 광고 파싱·검수 입력 생성기
```

기존 농협 원본 `1.awx_custom_parser_example_api`는 **KL이 외부 Custom Parser를 호출하는
통신 규격**이다. 이를 바탕으로 예전에 만든 `kl_parser`도 독립 FastAPI 서버다.

이번 ETLwithLLM 가이드 3장의 `custom_extension`은 **ETLwithLLM 프로세스 안에 Python
코드를 끼우는 플러그인 규격**이다. 이름이 비슷하지만 자동으로 연결되지 않는다.

## 2. 결론부터 보는 선택표

| 후보 | 구조 | 문서 근거 | 현재 상태 | 선택 조건 |
|---|---|---:|---|---|
| **A. 농협 내부 별도 프로세스/API** | ETL 컨테이너 밖의 우리 프로세스가 농협 내부 ETL HTTP API 호출 | 높음 | 실행 코드 완료 | ETL 컨테이너에 우리 코드를 넣지 않을 때 |
| **B. transform 확장** | ETL DLA 뒤에 우리 코드 실행 | 중간~높음 | 구현 후보 완료 | `transform()` 호출·출력 계약을 확인했을 때 |
| **C. CUSTOMIZE 프로브** | `extract()` 인자만 기록 | 낮음 | 비운영 프로브 완료 | `CUSTOMIZE`의 실제 후속 흐름을 확인할 때 |
| **KL Custom Parser** | KL이 `/ad/parsing` API 호출 | 별도 규격 | 현재 저장소용 선택적 진입점 구현 | 최상위 호출자가 KL일 때 |

현재 추천 순서는 **A를 기준선으로 두고 B를 우선 문의**하는 것이다. B가 공식 지원되면
배포가 단순해질 수 있다. C는 `extract()` 뒤에 DLA가 자동 실행된다는 확인을 받기 전에는
파이프라인 안으로 선택하지 않는다.

## 3. 가이드에서 확정된 것과 확정되지 않은 것

### 확정

- 외부 DLA 요청은 `POST /api/v1/etl/auto/start`다.
- form-data의 `tr_data` JSON 문자열과 반복 가능한 `upfiles`를 보낸다.
- 외부 `prj_config` 키는 p.29 원문의 **`extract_type`**이다.
- 공개 선택값은 `all`, `dla`, `parser`이고 기본값은 `dla`다.
- 내부 `etl_service.py`는 `CUSTOMIZE`일 때 `extract()`만 호출한다.
- `transform()`은 `input_file_path`와 `dla_result_path`를 받는 DLA 후처리 훅이다.
- Default JSON에는 페이지 크기, 객체·줄·표 좌표와 텍스트가 있다.

### 미확정

- 외부 API에서 `extract_type: "customize"`를 허용하는가.
- 허용할 경우 어떤 문자열이 내부 `ExtractType.CUSTOMIZE`로 매핑되는가.
- `extract()` 다음에 기본 DLA를 이어 호출할 공식 방법이 있는가.
- `transform()`이 `extract_type: "dla"`에서 항상 호출되는가.
- `dla_result_path`가 파일인지 디렉터리인지, 어느 JSON을 가리키는가.
- `transform()` 결과 파일을 어디에 써야 플랫폼 결과로 수집되는가.
- `CustomExtension`의 실제 import 경로와 `create()`의 구현체 탐색 규칙.

코드에서 미확정 항목은 `TODO`나 명시적 오류로 남겼다. 조용히 추정해서 실행하지 않는다.

## 4. 공통으로 구현한 파이프라인 경계

두 후보가 공유하는 핵심은 다음 함수다.

```text
process_file_from_dla(input_path, default_json)
```

동작:

```text
ETL Default JSON
  -> callback/data wrapper 정규화
  -> pages를 기존 PaddleXPageResult 모양으로 변환
  -> ETL 좌표를 우리 렌더 캔버스 크기로 변환
  -> 기존 영역 조립
  -> 기존 VLM 판독·분류
  -> 기존 템플릿·구분 라벨
  -> parse / P1 evidence / P3 review-input
```

이 경로에서는 기존 `pipeline.py`의 타일 분할과 `request_layout_parsing()`을 호출하지
않는다. 농협 DLA가 이미 레이아웃·OCR·표 분석을 수행했기 때문이다. 페이지 이미지는
좌표를 맞추고 VLM crop을 만들기 위해서만 렌더한다.

지원 범위는 우선 PDF/PNG/JPG 전체 페이지다. `start_page/end_page`로 일부 페이지만
분석한 결과와 HWP 직접 파싱은 방향 확정 뒤 별도로 설계한다.

## 5. A안 - 농협 내부의 별도 프로세스에서 ETL 공개 API 호출

여기서 `외부`는 인터넷·회사망을 뜻하지 않는다. 농협 폐쇄망 안에서 ETLwithLLM
컨테이너와 별도로 실행되는 우리 프로그램이 ETLwithLLM의 HTTP API를 호출한다는 뜻이다.

코드:

```text
src/nh_parser/integrations/etlwithllm/client.py
tools/etl_review_run.py
```

흐름:

```text
우리 실행기
  -> POST /etl/auto/start (extract_type=dla, res_type=default)
  -> GET /file/info 폴링
  -> GET /file/result/list
  -> GET /file/result/doc
  -> Default JSON
  -> 공통 파이프라인
```

로그인·워크스페이스 생성은 구현하지 않았다. 농협에서 받은 `ws_id`와 이미 인증된 세션을
주입하는 경계만 둔다. 클라이언트는 가이드에 없는 `customize`와 잘못된
`extractType` 키를 명시적으로 거부한다.

검토용 실행도 저장소 출력 규칙에 맞춰 다음처럼 사용한다.

```bash
uv run python tools/etl_review_run.py \
  --base-url http://ETL-HOST:PORT \
  --ws-id PROVIDED_WS_ID \
  --author USER \
  --input sample.pdf \
  --out out/2026-09-09/etl-external-review
```

기본 `--region-reading off`는 영역별 Reader/Judge만 끈다. 문서 분류에는 기존 VLM 호출이
남아 있으므로 전체 P1/P3 실행에는 농협 측 VLM 호출 설정이 필요하다. VLM 없이 확인할
범위는 단위 테스트와 `tools/etl_probe.py`의 ETL 계약 점검이다.

장점:

- 공개 API만 사용하므로 가장 검증 가능하다.
- ETL 컨테이너에 우리 의존성을 설치할 필요가 없다.
- ETL과 우리 파이프라인을 독립적으로 배포·디버깅할 수 있다.

단점:

- 업로드·폴링·결과 조회 코드가 필요하다.
- 우리 실행기와 ETL 사이 네트워크 경로가 필요하다.
- 처리 결과를 어느 상위 시스템에 어떻게 반환할지는 별도 결정이다.

## 6. B안 - `transform()` custom extension

후보 파일:

```text
custom-extension-transform/
└── app/custom_extension/implements/
    └── base_custom_extension.py
```

흐름:

```text
ETLwithLLM
  -> 기본 DLA/STR/TSR
  -> transform(input_file_path, dla_result_path, ...)
  -> 공통 파이프라인
```

이 후보의 `extract()`는 의도적으로 오류를 낸다. 잘못해서 `CUSTOMIZE`를 선택하면 농협
DLA 없이 파이프라인이 진행되는 것을 막기 위해서다.

`dla_result_path`는 문서가 파일·디렉터리 여부를 밝히지 않아 둘 다 읽도록 구현했다.
디렉터리인 경우 실제 JSON을 열어 Default 구조인 파일만 고르고, 여러 개면 `pdfName`이
원본명과 같은 결과를 선택한다.

장점:

- 결과가 로컬 파일 경로로 들어와 외부 폴링 코드가 필요 없다.
- 플랫폼 작업 생명주기 안에서 후처리를 실행할 수 있다.

단점:

- ETL 컨테이너에 `nh_parser`와 모든 Python/system 의존성을 넣어야 한다.
- 플랫폼 프로세스의 메모리·시간·장애 범위에 우리 코드가 포함된다.
- import 경로, 취소 API, 결과 수집 경로가 가이드에 없다.

## 7. C안 - `CUSTOMIZE/extract()` 계약 프로브

후보 파일:

```text
customize-extract-probe/
└── app/custom_extension/implements/
    └── base_custom_extension.py
```

이 코드는 다음 파일 하나만 쓴다.

```text
cginside_customize_probe.json
```

기록 내용은 입력 경로, 출력 경로, 파일 존재·크기, `digitize_config`다. OCR이나 DLA를
호출하지 않는다. 목적은 `CUSTOMIZE`가 실제로 선택 가능한지, 어떤 값이 전달되는지,
`extract()` 뒤에 플랫폼 후속 단계가 존재하는지를 관찰하는 것이다.

이 후보를 운영 분석에 사용하면 안 된다. p.53 분기에는 `dla_task()`가 없기 때문이다.

## 8. 기존 KL Custom Parser와의 차이

대조 기준은 농협 제공 `1.awx_custom_parser_example_api`와 이전
`nh-ad-review-poc/kl_parser` 원본이다. 두 자료는 이 저장소에 복사하지 않았으며,
현재 이식 범위와 차이는 아래 표 및 `docs/kl-parser-선택적-api.md`에 기록했다.

| 항목 | KL Custom Parser | ETL 외부 API A안 | custom extension B/C안 |
|---|---|---|---|
| 누가 호출하나 | Knowledge Lake | 우리 실행기 | ETLwithLLM 내부 |
| 진입점 | `POST /parsing` | `/api/v1/etl/auto/start` | Python 메서드 훅 |
| 완료 방식 | UUID polling 후 ZIP | file_path polling 후 JSON | 같은 ETL 작업 내부 |
| 주 결과 | `_hrc.jsonl`, `_hrc.json`, ZIP | Default JSON | 우리가 저장한 후처리 결과 |
| OCR 위치 | 파서가 공급자 API 호출 | ETLwithLLM이 수행 | B는 ETL DLA, C는 수행 안 함 |
| 별도 서버 | 필요 | 우리 실행기는 필요 | 불필요 |

따라서 `kl_parser`는 `implements/api/`와 같은 코드가 아니다. 필요하다면 다음처럼 결합할
수 있지만, 최상위 호출 주체가 KL이라는 요구가 있을 때만 선택한다.

```text
KL -> kl_parser -> ETL 외부 API(A) -> 공통 파이프라인 -> KL ZIP
```

반대로 ETL 작업 자체가 최상위 진입점이라면:

```text
ETL 요청 -> DLA -> transform(B) -> 우리 결과
```

이므로 `kl_parser`가 필요하지 않을 수 있다. 과거 구현은 수정하지 않았고, 현재 저장소에
광고 전용 선택적 진입점 `src/nh_parser/kl_api/`를 별도로 구현했다. `local` 백엔드는
현재 DGX 파이프라인을, `etl` 백엔드는 A안을 호출한다. 일반 문서 RAG용 `/parsing`은
현재 저장소에 `rag_ingest`/`kl_export`가 없으므로 명시적으로 501을 반환한다. 자세한
실행법은 `docs/kl-parser-선택적-api.md`를 참고한다.

## 9. `implements/api/`는 언제 쓰나

`implements/api/`는 ETL 서버에 별도 FastAPI `APIRouter`를 등록할 때만 필요하다.
`transform()`이 자동 실행된다면 주 파이프라인을 위해 API를 추가할 이유가 없다.

가능한 사용 사례:

- 우리 결과 상태 조회
- 특정 문서 후처리 재실행
- 진단·health endpoint

현재 B/C 후보의 `get_custom_routers()`는 빈 리스트를 반환한다. 현재 저장소의
`kl_api`는 ETL 컨테이너 밖에서 실행하는 별도 FastAPI 앱이며 `implements/api/`로
자동 등록되는 코드가 아니다.

## 10. 마운트와 배포 이미지

가이드의 volume mount는 우리 소스 디렉터리를 ETL 컨테이너의
`app/custom_extension/implements`로 보이게 하는 것이다. 우리 코드를 별도 컨테이너로
만드는 의미가 아니다.

```text
농협 호스트의 CUSTOM_EXTENSION_PATH
  -> ETL 컨테이너 /just-type-ewl-app/app/custom_extension/implements
```

마운트는 소스만 보이게 하고 라이브러리를 설치하지 않는다. `nh_parser` wheel,
Pillow, pypdfium2 등은 ETL 실행 이미지에 별도로 설치되어야 한다.

이미지 빌드 시 코드를 해당 위치에 포함했다면 compose mount를 주석 처리해야 한다.
그렇지 않으면 외부 디렉터리가 이미지 안의 코드를 가린다.

## 11. 팀장님께 확인받을 결정

1. 최상위 호출자는 KL인가, ETLwithLLM인가, 별도 우리 서비스인가.
2. ETL 컨테이너 내부 코드 설치(B)를 허용하는가.
3. `transform()`이 `extract_type=dla`에서 호출된다는 공급사 확인을 받을 수 있는가.
4. B가 가능하면 결과 저장·수집 경로와 의존성 반입 방식을 제공받을 수 있는가.
5. B가 불가능하면 A의 외부 실행기를 어느 농협 내부 서버에서 운영할 것인가.
6. KL 적재 계약까지 이번 범위라면 기존 `kl_parser`를 현재 패키지에 맞춰 별도 이식할 것인가.
7. `CUSTOMIZE`는 계약 확인용 C 프로브만 유지할지, 공급사 답변 후 전처리안으로 확장할지.

## 12. 검증

네트워크 없이 확인하는 테스트:

```bash
uv run pytest tests/test_etl_dla_convert.py tests/test_etl_integration_candidates.py
```

검증 범위:

- Default JSON 직접형·`data`·`doc_result`·`doc_result.default` 정규화
- 문서에 없는 `extract_type=customize` 차단
- 외부 API endpoint와 query parameter 계약
- ETL 좌표를 우리 캔버스로 변환
- PaddleX를 호출하지 않고 기존 영역 조립으로 연결

실제 입력 파싱 테스트는 저장소 지침대로 `out/<YYYY-MM-DD>/<label>/`에 `--preview`를
사용한다. 현재 후보는 방향 결정용이므로 농협 실서버 결과를 얻기 전 성능을 확정하지 않는다.
