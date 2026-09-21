# parser-v2 실험 환경

spark-1118의 서버 파이프라인 YAML 결과에서 시작해 Region 소유권, 상품 그룹, 내부 정렬,
fc87 Gemma 판독·Judge, 템플릿 복수 라벨, P1/P3까지 단계적으로 연결하는 실험 공간이다.

Region 텍스트는 PaddleX `block_content`와 좌표 기반 OCR/디지털 줄 중 하나를 미리 버리지
않는다. PDF 내장 텍스트는 VLM이 다르게 읽어도 원문을 정본으로 보존하고, 이미지 OCR은
VLM Reader/Judge가 교정할 수 있다. 표는 모든 OCR/PDF 줄이 셀 또는 주석에 배치됐을 때만
격자 표현을 정본으로 쓴다. 상품 소유권 필드는 `product_id` 하나만 사용하고 `card_no`는
새 결과에 만들지 않는다.

설계와 단계별 완료 조건은
[`../파싱-파이프라인-고도화-계획.md`](../파싱-파이프라인-고도화-계획.md)를 따른다.

## 원칙

- `src/nh_parser`의 검증된 입력·OCR·VLM·review 함수를 import해 재사용한다.
- 기존 코드를 이 폴더에 복사하지 않는다.
- 단계마다 JSON과 Label Studio 파일을 `outputs/<run-name>/`에 남긴다.
- `outputs/`는 고객 원문과 내부 모델 응답이 포함되므로 Git에서 제외한다.
- 실제 주소와 모델명은 커밋하지 않는다.

## 실행 프로필 준비

`profile.env.example`을 참고해 저장소 루트의 기존 `.env` 또는 현재 PowerShell 프로세스에
값을 둔다. OCR과 VLM은 서로 다른 URL을 사용하므로 같은 프로세스에서 1118과 fc87을
각각 호출할 수 있다.

```text
PADDLEX_URL → 로컬 SSH tunnel을 통한 spark-1118
GEMMA_URL   → 기존 fc87 Gemma OpenAI 호환 endpoint
```

spark-1118 포트가 loopback에만 공개되어 있으므로 실행 중에는 별도 PowerShell 창에서
다음 터널을 유지한다.

```powershell
ssh -N -L 18081:127.0.0.1:8081 spark-1118
```

## 첫 구현 묶음

1. 입력 로드와 페이지 렌더
2. 종횡비 2.0 기준 통짜/타일 분기
3. spark-1118 호출 — `fileType` 외 선택 옵션은 보내지 않고 서버 YAML 사용
4. 페이지 좌표 복원과 타일 중복 제거
5. `block_content`·OCR·디지털 본문 후보 보존과 줄 단일 귀속
6. Label Studio `raw / assigned / unassigned` 출력

이 묶음에는 Gemma 호출을 넣지 않는다. 결정론 단계가 확인된 뒤 상품 `product_id`부터
Gemma를 연결한다.

## 1차 실행

1118 SSH 터널을 연 상태에서 저장소 루트에서 실행한다.

```powershell
uv run --cache-dir .uv-cache-codex python experiments/parser-v2/run.py `
  --run-name v2-baseline `
  --input samples/spark1118-sample
```

결과는 `experiments/parser-v2/outputs/v2-baseline/`에 생성되고 Git에는 포함되지 않는다.
`labeling-config.xml`과 `label-studio.json`을 Label Studio 프로젝트에 넣으면
원본 parsing 영역, 선택된 본문 출처, 미배정 줄을 따로 볼 수 있다.

fc87 Gemma 의미 판정과 최종 P1/P3까지 한두 입력만 실행할 때는 `--with-vlm`을 붙인다.

```powershell
uv run --cache-dir .uv-cache-codex python experiments/parser-v2/run.py `
  --run-name v2-full-smoke `
  --input "samples/spark1118-sample/4. 카드상품.pdf" `
  --with-vlm
```

추가 산출물은 다음과 같다.

```text
03-ownership.json  product_id와 내부 처리 순서(중간 진단용)
04-vlm-evidence.json  분류·템플릿·페이지 의미 판정 원문
05-p1.json       모든 OCR/PDF/VLM 근거와 복구 이력
06-p3.json       심의 단계 입력: Region별 bbox/product_id/복수 labels/selected_text
final/*.p3.json  문서별 P3
vlm-stats.json   fc87 Gemma schema별 호출 수·캐시·시간
label-studio.json의 `4-p3-semantic` 탭  product_id·복수 라벨·Region bbox 시각 확인
```

VLM이 어떤 OCR/PDF 텍스트를 `decorative`로 판단해도 해당 텍스트와 bbox를 삭제하지 않는다.
상세 판단과 후보는 P1에 남기고 P3에는 같은 `region_id`, 최종 텍스트, bbox와
`needs_review`만 전달한다. VLM은 제공된 ID를 선택할 뿐 새로운 bbox를 만들지 않는다.

## P3 Region 계약

P3는 영역을 라벨별 자식 영역으로 다시 쪼개지 않는다. 레이아웃에서 얻은 큰 Region을
유지하고 `labels` 배열에 해당하는 구분값을 모두 붙인다. 화면에서는 `bbox`로 대략적인
위치를 보여 주며, 더 자세한 근거가 필요하면 같은 `region_id`의 P1 OCR/PDF 줄 좌표를
조회한다.

```json
{
  "region_id": "p1_r020",
  "product_id": "product_1",
  "bbox": [243, 518, 1570, 708],
  "selected_text": "...",
  "labels": ["가입대상", "금리"],
  "kind": "table",
  "needs_review": true,
  "text_source": "ocr",
  "table": {"grid": {}, "cells": [], "notes": []}
}
```

`text_source`는 `ocr` 또는 `vlm`만 사용한다. 세부 후보와 판정 이력은 P1에 남는다.
이 구조의 계약 버전은 `nh-ad-region-review-input-v4`다.

## `needs_review`의 의미

이 값은 **광고 심의 결과가 아니다.** 파싱 파이프라인이 최종 텍스트·표 구조·상품 소유권
또는 라벨을 자동 확정하기 어려워 원문 대조가 필요하다는 품질 신호다. 심의 단계는 이 값을
참고할 수 있지만 `위반/충족/판정불가`는 별도로 판정해야 한다.

P3에는 boolean만 보내고, 원인은 P1의 같은 Region에 있는 `review_reasons`에서 확인한다.

| 사유 코드 | 의미 |
| --- | --- |
| `digital_text_vlm_disagreement` | PDF 내장 텍스트와 VLM 판독이 달라 PDF 원문을 보존함 |
| `ocr_vlm_disagreement` | 이미지 OCR과 VLM 판독이 다르고 Judge를 확정하지 못함 |
| `vlm_only_text` | OCR/PDF 글자 좌표 없이 VLM만 글자를 읽음 |
| `vlm_read_failed` | Region VLM 판독 호출 실패 |
| `vlm_judge_low_confidence` | Judge 신뢰도가 0.7 미만 |
| `table_unplaced_lines` | 표 셀 또는 주석에 들어가지 못한 OCR/PDF 줄이 있음 |
| `table_low_confidence` | 표 구조 신뢰도가 0.7 미만 |
| `ownership_low_confidence` | 상품 소유권 신뢰도가 0.7 미만 |
| `ownership_unknown` | 어느 상품에 속하는지 확정하지 못함 |
| `recovery_action_uncertain` | 미배정 줄 처리 방식을 확정하지 못함 |
| `recovery_low_confidence` | 미배정 줄 처리 신뢰도가 0.7 미만 |
| `template_unresolved` | 상품 템플릿을 확정하지 못함 |
| `label_low_confidence` | 구분값 라벨 신뢰도가 0.7 미만 |

라벨이 없다는 사실만으로 `needs_review=true`가 되지는 않는다. 광고 수식 문구처럼 어느
템플릿 구분값에도 해당하지 않는 정상 Region이 있기 때문이다. 대신 템플릿 용어 별칭은
결정론 규칙으로 보완한다. 현재 `이자지급방식/방법/주기`는 `이자지급시기`로 정규화한다.

## 페이지 전체 VLM과 Region VLM의 역할

- 페이지 전체 또는 긴 페이지 의미 밴드: 상품 경계, Region 소유권, 흩어진 표 조각,
  `field_list`, 공통 고지를 찾는다.
- 표 구조: 빨간 대상 박스를 표시한 페이지 전체 이미지와 표 상세 crop을 함께 본다.
- 상품별 라벨링: 페이지 전체 박스 이미지와 각 Region 확대 시트를 함께 본다.
- 글자 전사: 이웃 문장이 섞이지 않도록 bbox 밖을 가린 Region crop만 본다.

VLM은 표·문단의 **관계와 의미**를 판단하고, 최종 글자와 좌표는 OCR/PDF 근거에서
조립한다. 상품 소유권을 정하기 전에는 미배정 줄을 큰 표로 미리 합치지 않으므로 좌우의
서로 다른 상품 표가 하나가 되는 일을 막는다.

일반 페이지의 의미 판정은 페이지 전체 1회다. OCR 단계에서 긴 페이지로 판정된 입력은
같은 축을 2~4개 문맥 밴드로 나누며, 각 Region/복구 후보 ID는 중심점 기준으로 정확히 한
밴드에만 들어간다. crop은 주변 문맥만 겹치고 판정 소유권은 겹치지 않는다.

터널 없이 이미 저장한 `merge-large` 결과로 Region 조립만 재검증하려면 다음을 쓴다.

```powershell
uv run --cache-dir .uv-cache-codex python experiments/parser-v2/replay.py `
  --lab-run layout-20260918-v1--merge-large `
  --run-name v2-replay-large
```

## 현재 기준선 — 서버 YAML만 사용

2026-09-19에 `v2-server-yaml-live`로 49개 입력·51페이지를 다시 실행했다.

- 실제 HTTP 선택 옵션은 `fileType` 하나뿐이다.
- Region 1,206개, 정본 텍스트 줄 3,838개, 미배정 줄 199개다.
- 본문 후보 충돌은 85개다. 서버 YAML의 표 인식이 켜져 `block_content` 후보가 늘어난 영향이다.
- 과거 HTTP `large` 실행과 비교하면 **51페이지 전체의 Region bbox·layout label은 동일**했다.
- 17페이지에서 선택 본문 후보가 달랐고, 전체 시간은 164.3초에서 253.8초로 늘었다.

```text
experiments/parser-v2/outputs/v2-server-yaml-live/
```

manifest의 아래 두 값이 서버 설정만 사용했다는 증거다.

```json
{
  "request_payload": {"fileType": 1},
  "paddlex_options_source": "server_pipeline_yaml"
}
```

## 과거 비교 기준선 — HTTP `large` override

2026-09-19에 `samples/spark1118-sample` 전체를 아래 조건으로 실행했다. 이 실행은
`layoutMergeBboxesMode=large`를 HTTP 요청에서 보낸 **과거 비교 기준선**이다. 이후 실행은
선택 옵션을 보내지 않고 spark-1118 YAML만 사용한다.

- 입력 49개, 51페이지
- 일반 페이지는 한 장 그대로 호출하고 종횡비가 2.0을 넘는 7페이지는 높이 1600px 타일 사용
- 과거 실행은 `layoutMergeBboxesMode=large`, 표·수식·방향 보정을 요청에서 덮어씀
- Region 1,206개, 정본 텍스트 줄 3,837개
- Region에 들어가지 않은 정본 줄 199개, 본문 후보 충돌 17개
- 긴 페이지 타일 경계 OCR 중복은 좌표로 합치고, 탈락 판독은 `tile_alternates`에 보존

결과 디렉터리는 다음과 같다. 원문과 모델 응답이 있으므로 Git에는 포함되지 않는다.

```text
experiments/parser-v2/outputs/v2-baseline-live/
```

상태 요약을 다시 만들려면 다음 명령을 쓴다.

```powershell
uv run --cache-dir .uv-cache-codex python experiments/parser-v2/summarize.py `
  experiments/parser-v2/outputs/v2-server-yaml-live
```

## Label Studio에서 직접 확인

브라우저에서 <http://localhost:8080>만 사용한다. `127.0.0.1`과 섞어 쓰면 로그인 쿠키가
달라져 다시 로그인할 수 있다.

1. 새 프로젝트 `parser-v2 server yaml live`를 만든다.
2. `Settings → Labeling Interface → Code`에 아래 파일 내용을 붙여 넣고 저장한다.

   ```text
   experiments/parser-v2/outputs/v2-server-yaml-live/labeling-config.xml
   ```

3. `Settings → Cloud Storage → Add Source Storage → Local Files`에서
   `/label-studio/files/pages`를 등록한다. 이미 같은 저장소가 등록되어 있으면 재사용한다.
4. `Data Import`에서 아래 파일을 올린다.

   ```text
   experiments/parser-v2/outputs/v2-server-yaml-live/label-studio.json
   ```

한 task의 prediction 선택기는 다음 뜻이다.

| prediction | 확인할 내용 |
| --- | --- |
| `1-raw-parsing` | PaddleX가 반환한 원래 parsing 영역과 클래스 |
| `2-canonical-regions` | 같은 영역에 최종 선택된 본문 출처. 영역 경계 비교용이 아니라 텍스트 근거 확인용 |
| `3-unassigned` | OCR/PDF 텍스트는 있으나 어느 Region에도 들어가지 않은 줄 |

`3-unassigned`가 있다고 곧바로 오류는 아니다. 장식·상표처럼 버릴 줄인지, 기존 Region에
붙일 줄인지, 별도 복구 Region으로 만들 줄인지를 사람이 구분해야 한다. 먼저 아래 순서로 본다.

1. 미배정이 많은 `14. 대출성상품.pdf`, `3/4. 예금성상품(입출식).pdf`,
   `4. 예금성상품(적립식).pdf`, `2. 예금성상품(거치식).pdf`
2. 본문 후보 충돌이 많은 `2. 카드상품.pdf`, `23-1. 대출성상품.png`
3. 여러 상품의 소유권을 봐야 하는 `1/2/3/4/8. 카드상품`
4. 타일 경계를 봐야 하는 `올원e적금`, `NH농협은행-2026_001/002-예금성`,
   `23-1. 대출성상품.png`

확인 메모는 다음 네 가지만 적으면 후처리 규칙으로 옮길 수 있다.

```text
파일명 / 페이지 / prediction / 위치 또는 보이는 문구
판정: 기존 Region에 붙임 | 별도 Region 필요 | 장식이라 무시 | 영역을 나눠야 함
```

## 기준선 확인 뒤의 구현 순서

1. 미배정 줄을 무조건 가까운 영역에 넣지 않고 `붙임/복구 Region/무시` 규칙으로 처리한다.
2. 한 페이지 문맥을 함께 본 Gemma 호출로 Region마다 `product_id`를 정한다.
3. 일반 페이지는 전체 이미지를 문맥으로 쓰고, 긴 페이지는 저해상도 전체 보기와 타일/Region
   crop을 함께 써 상품 경계를 잃지 않게 한다.
4. 내부 처리에서는 상품 그룹을 고려해 Region 순서를 정리하되 P3에는 별도 순서 필드를 싣지 않는다.
5. 각 비표 Region을 Reader가 독립 판독하고, OCR/PDF 후보와 다를 때만 Judge가 최종 텍스트를 정한다.
6. Region을 span으로 나누지 않고 해당하는 템플릿 구분값을 `labels` 배열에 모두 붙인다.
7. P1에는 모든 근거를 보존하고, P3에는 같은 `region_id`의 최종 텍스트·bbox·복수 라벨만 전달한다.
