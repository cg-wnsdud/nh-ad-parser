# parser-v2 실험 환경

spark-1118의 `merge-large` OCR 결과에서 시작해 Region 소유권, 상품 그룹, 읽기 순서,
fc87 Gemma 판독, 템플릿 단일 라벨, P1/P3까지 단계적으로 연결하는 실험 공간이다.

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
5. OCR 줄 단일 귀속과 미배정 보존
6. Label Studio `raw / assigned / unassigned` 출력

이 묶음에는 Gemma 호출을 넣지 않는다. 결정론 단계가 확인된 뒤 상품 `group_id`부터
Gemma를 연결한다.
