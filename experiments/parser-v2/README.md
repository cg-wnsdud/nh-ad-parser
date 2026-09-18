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
