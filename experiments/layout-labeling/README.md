# NH 영역 정답 라벨링 환경

Label Studio는 정답 박스와 관계를 사람이 기록하는 도구다. PaddleX 파라미터를 바꾸거나
모델을 실행하지 않는다. 추론은 spark-1118, 정답 작성과 평가는 로컬에서 분리한다.

Compose image는 안정 릴리스 `1.23.0`과 확인한 registry digest
`sha256:aa461572e8f9d86a1bf9520c1db620204e86160fd2f80dd7e9d40ac84a8828ea`로 고정했다.

## 1. 입력 페이지와 task JSON 만들기

저장소 루트에서 실행한다.

```powershell
uv run python tools/prepare_layout_labeling.py --input samples
```

생성물은 전부 Git ignore 대상이다.

```text
experiments/layout-labeling/.local/media/   Label Studio가 표시할 원본 좌표계 PNG
experiments/layout-labeling/tasks/tasks.json
experiments/layout-labeling/tasks/manifest.json
```

## 2. 실행

```powershell
docker compose -f experiments/layout-labeling/docker-compose.yml up -d
docker compose -f experiments/layout-labeling/docker-compose.yml ps
```

브라우저에서 반드시 <http://localhost:8080>을 연다. 최초 실행이면 로컬 계정을 만든다.
Compose의 `LABEL_STUDIO_HOST`도 이 주소이므로 `http://127.0.0.1:8080`과 섞어 쓰지 않는다.
두 호스트를 섞으면 브라우저 로그인 쿠키가 API 요청에 실리지 않아 로그인 화면이 반복해서
나타날 수 있다.

Label Studio 1.23.0은 파일이 컨테이너에 마운트돼 있는지만 확인하지 않는다. 프로젝트가 그
파일을 읽을 권한이 있는지도 `Local Files` 저장소 등록으로 검사한다. 프로젝트를 만든 뒤 다음
경로를 소스 저장소로 한 번 등록한다.

```text
Project Settings → Cloud Storage → Add Source Storage → Local Files
Storage title: NH labeling pages
Absolute local path: /label-studio/files/pages
```

이 등록을 생략하면 `/label-studio/files/pages`에 PNG가 실제로 있어도
`/data/local-files/?d=pages/...` 요청은 `404`가 되고 라벨 화면에는 이미지 로드 오류가 나타난다.
저장소 동기화로 task를 새로 만들 필요는 없다. 아래의 JSON Data Import를 사용할 때는 프로젝트의
파일 읽기 권한을 연결하는 용도다.

## 3. 프로젝트를 두 개로 나누는 이유

### A. PP 레이아웃 프로젝트

`labeling-config-pp-layout.xml`을 붙여 넣는다. PP-DocLayout_plus-L의 20개 클래스와 같은
기준으로 정답 박스를 그린다. threshold, NMS, unclip, merge 및 모델 교체를 비교할 때 쓴다.

### B. NH 구조 프로젝트

`labeling-config-nh-structure.xml`을 붙여 넣는다. 상품 패널, 혜택, CTA, 고지와
`belongs_to` 관계를 표시한다. PP 모델에 없는 광고 업무 구조를 평가할 때 쓴다.

두 프로젝트 모두 `tasks/tasks.json`을 Data Import에서 불러온다. 같은 박스에 PP 클래스와
NH 의미를 억지로 함께 넣지 않는다.

## 4. spark-1118 기준선에 연결

PaddleX는 1118 loopback에만 열려 있으므로 별도 PowerShell 창에서 SSH tunnel을 유지한다.

```powershell
ssh -N -L 18081:127.0.0.1:8081 spark-1118
```

일반 parser를 1118로 실행할 때는 process 환경변수를 지정한다. `.env`의 fc87 값을 바꾸지 않아도
PowerShell에서 지정한 값이 우선한다.

```powershell
$env:PADDLEX_URL = "http://127.0.0.1:18081/layout-parsing"
```

먼저 한 이미지의 global request parameter 반응을 확인할 수 있다.

```powershell
uv run python tools/probe_paddlex_params.py `
  --image "samples/[예금성상품-적금] 올원e적금.png" `
  --out "tmp/probe-1118-base.json"
```

`layoutMergeBboxesMode`의 class별 YAML을 평가할 때는 client가 `small`을 보내지 않도록
`PADDLEX_LAYOUT_MERGE_BBOXES_MODE=server`를 사용한다. threshold와 unclip도 `server`면 해당
request key를 생략한다.

### 사전 렌더 PNG와 실제 파이프라인 실행의 차이

이 폴더의 PNG는 Label Studio가 표시할 **정답 좌표 캔버스**다. PDF triage와 렌더링은 현재 parser와
같지만, 긴 페이지 타일링과 PaddleX 호출은 아직 하지 않은 상태다.

- server 자체만 비교할 때: 이 PNG를 한 장 그대로 `/layout-parsing`에 보낸다.
- 실제 NH 영역 결과를 비교할 때: 원본 PDF/이미지에서 시작해 parser의
  `triage → render → tile → PaddleX → 좌표 복원 → dedupe → Region 조립`을 실행하고 VLM은 끈다.
- 최종 품질 회귀 때: 위 단계가 안정된 뒤 VLM까지 포함한다.

긴 페이지는 실제 parser가 높이 최대 1600px tile로 자르므로, 사전 렌더 PNG 한 장을 직접 보낸 결과와
다를 수 있다. Label Studio PNG를 만든 목적은 추론을 대신하려는 것이 아니라 사람이 그린 bbox와 parser의
페이지 bbox가 같은 픽셀 좌표계를 쓰게 하려는 것이다.

## 5. 이미지 한 장을 spark-1118에서 실행해 Label Studio로 확인

### 5.1 SSH tunnel 유지

PowerShell 창 하나에서 다음 명령을 실행한 채 둔다. 아무 출력 없이 계속 실행되는 것이 정상이며, 창을
닫으면 tunnel도 종료된다.

```powershell
ssh -N -L 18081:127.0.0.1:8081 spark-1118
```

### 5.2 원본부터 VLM 직전까지 실행

다른 PowerShell 창에서 실행한다.

```powershell
uv run python tools/run_layout_preview.py `
  --input "samples/[예금성상품-적금] 올원e적금.png"
```

이 명령은 로컬 `.env`의 fc87 주소를 수정하지 않는다. 이번 process에만 1118 tunnel 주소를 적용한다.

```text
원본 입력 → RGB/PDF 렌더 → 긴 페이지 tile → spark-1118 PaddleX
          → 좌표 복원 → line/block dedupe → Region 조립
```

카드 분할, 누락 문구 sweep, 문서 분류를 포함한 VLM 호출은 전부 생략한다. 기본
`--merge-mode server`는 `layoutMergeBboxesMode`를 HTTP 요청에서 빼 1118 base YAML의 클래스별
dict를 사용한다. `--merge-mode small`을 주면 기존 client의 scalar override를 시험한다.

산출물은 Git ignore 경로에 생긴다.

```text
experiments/layout-labeling/tasks/runs/spark-1118-base/parse.json
experiments/layout-labeling/tasks/runs/spark-1118-base/preview/
experiments/layout-labeling/tasks/runs/spark-1118-base/label-studio-import.json
```

### 5.3 Label Studio에서 확인

PP 레이아웃 프로젝트의 Data Import에서 다음 파일을 올린다.

```text
experiments/layout-labeling/tasks/runs/spark-1118-base/label-studio-import.json
```

예측 박스는 `predictions`로 들어간다. 원본을 기준으로 경계를 수정해 정답 annotation으로 확정한다.
NH 상품 패널·혜택·CTA 관계는 별도의 NH 구조 프로젝트에서 사람이 라벨링한다.

## 6. 데이터 원칙

- PDF는 현재 파이프라인과 같은 렌더링 분기로 PNG를 만든다. Label Studio에서 보이는 픽셀과
  파서 좌표가 같아야 IoU가 의미가 있다.
- `decorative`는 평가에는 존재하지만 업무 추출 대상이 아닌 장식이다.
- `exclude`는 흐림, 잘림 등으로 사람이 정답을 확정할 수 없는 곳에만 쓴다.
- 예측을 보고 그대로 승인하지 말고 원본을 기준으로 경계를 고친다.
- export JSON은 `exports/`에 두며 고객 문구와 좌표가 들어 있으므로 커밋하지 않는다.

## 7. 종료와 로그

```powershell
docker compose -f experiments/layout-labeling/docker-compose.yml logs --tail 100
docker compose -f experiments/layout-labeling/docker-compose.yml down
```

`down`은 컨테이너만 내린다. SQLite와 업로드 상태는 `.local/data`에 남는다. `down -v`나
`.local/data` 삭제는 라벨링 상태를 잃을 수 있으므로 사용하지 않는다.
