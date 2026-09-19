# parser-v2 실험 환경

spark-1118의 `merge-large` OCR 결과에서 시작해 Region 소유권, 상품 그룹, 읽기 순서,
fc87 Gemma 판독, 템플릿 단일 라벨, P1/P3까지 단계적으로 연결하는 실험 공간이다.

Region 텍스트는 PaddleX `block_content`와 좌표 기반 OCR/디지털 줄 중 하나를 미리 버리지
않는다. 대부분 일치하면 `block_content`를 쓰고, 빈 표는 줄로 보완하며, 충돌은 VLM/Judge
대상으로 표시한다. 상품 소유권 필드는 `product_id` 하나만 사용하고 `card_no`는 새 결과에
만들지 않는다.

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
3. spark-1118 merge-large 호출
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

터널 없이 이미 저장한 `merge-large` 결과로 Region 조립만 재검증하려면 다음을 쓴다.

```powershell
uv run --cache-dir .uv-cache-codex python experiments/parser-v2/replay.py `
  --lab-run layout-20260918-v1--merge-large `
  --run-name v2-replay-large
```

## 검증 완료 기준선

2026-09-19에 `samples/spark1118-sample` 전체를 아래 조건으로 실행했다.

- 입력 49개, 51페이지
- 일반 페이지는 한 장 그대로 호출하고 종횡비가 2.0을 넘는 7페이지는 높이 1600px 타일 사용
- `layoutMergeBboxesMode=large`, 표·수식·방향 보정은 요청에서 끔
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
  experiments/parser-v2/outputs/v2-baseline-live
```

## Label Studio에서 직접 확인

브라우저에서 <http://localhost:8080>만 사용한다. `127.0.0.1`과 섞어 쓰면 로그인 쿠키가
달라져 다시 로그인할 수 있다.

1. 새 프로젝트 `parser-v2 baseline live`를 만든다.
2. `Settings → Labeling Interface → Code`에 아래 파일 내용을 붙여 넣고 저장한다.

   ```text
   experiments/parser-v2/outputs/v2-baseline-live/labeling-config.xml
   ```

3. `Settings → Cloud Storage → Add Source Storage → Local Files`에서
   `/label-studio/files/pages`를 등록한다. 이미 같은 저장소가 등록되어 있으면 재사용한다.
4. `Data Import`에서 아래 파일을 올린다.

   ```text
   experiments/parser-v2/outputs/v2-baseline-live/label-studio.json
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
2. 한 페이지 문맥을 함께 본 Gemma 호출로 Region마다 `product_id`와 템플릿 라벨 하나를 정한다.
3. 일반 페이지는 전체 이미지를 문맥으로 쓰고, 긴 페이지는 저해상도 전체 보기와 타일/Region
   crop을 함께 써 상품 경계를 잃지 않게 한다.
4. 상품 그룹 안에서만 읽기 순서를 정렬하고 명백한 역전만 고친다.
5. 1,206개 전부를 다시 읽히지 않고, 충돌 17개와 금리·금액 등 중요 영역만 Reader/Judge로
   교차 판독한다.
6. 최종적으로 기존 P1/P3 형식에 `product_id`와 단일 라벨 결과를 연결한다.
