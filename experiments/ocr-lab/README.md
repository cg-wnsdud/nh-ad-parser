# ocr-lab — 한 장 = 한 호출

기존 파이프라인 위에 계속 고치는 대신, **입력 처리만 남기고 백지에서** 다시 시작한 실험
환경이다. `src/nh_parser/` 는 한 줄도 건드리지 않고 읽기만 한다.

## 왜 새로 시작했나

초기 설계는 "세로로 긴 이미지를 통짜로 넣으면 뭉개진다"는 **사례 하나**를 풀려고 만든
것이었다. 그 해법(높이 4000px 초과 시 가로 띠로 자르기)이 이후 **모든 입력에 적용되는
일반 규칙**이 됐고, A4도 세로 광고도 아닌 자유 규격은 고려 대상이 아니었다.

2026-09-17 실측에서 그 대가가 드러났다.

| 파일 | 실물 크기 | triage | 적용 DPI | 캔버스 | 타일 |
| --- | --- | --- | --- | --- | --- |
| 2. 카드상품 | 1500 × 1500 mm | `scan_like` | 72 | 4251×4251 | 6 |
| 1. 카드상품 | 1600 × 1400 mm | `structured` | **200** | **12598×11024** | **16** |

거의 같은 크기의 포스터인데 캔버스 면적이 7.7배 차이났다. 이유는 **한쪽에 텍스트
레이어가 있었다는 것뿐**이다. `1. 카드상품` 은 12599×245 같은 51:1 띠로 쪼개져 모델에
들어갔고, 그 띠들은 전부 800×800 정사각형으로 눌렸다.

### 원인 — 무관한 질문들이 사슬로 엮여 있었다

```text
triage 판정  ──→  렌더 DPI  ──→  캔버스 픽셀  ──→  타일링 발동
(텍스트 레이어가         (200 or 72~200)            (높이 > 4000?)
 쓸만한가?)
```

triage 는 **글자 데이터의 유무**를 묻는데, 그 답이 **해상도**를 정하고 있었다. 두 질문은
서로 무관하다.

## 이 환경의 규칙

1. **triage 는 기록만 한다.** 크기 결정에 쓰지 않는다. `run.json` 의 `origin` 에 남는다.
2. **크기는 `--sizing` 하나가 모든 입력에 똑같이 적용한다.**
3. **타일링을 하지 않는다.** 한 페이지 = 한 번 호출.
4. **응답을 가공하지 않는다.** `raw/` 에 `prunedResult` 가 그대로 저장된다.

### 재사용 / 폐기

| 재사용 (import 만) | 폐기 |
| --- | --- |
| `ingest.canvas` — PDF 렌더, 이미지 로드 | `pipeline.py` 전부 |
| `ingest.triage` — 판정 (**기록 전용**) | `ocr/tiling.py`, `ocr/bands.py` |
| `ingest.assets` — HWP 내장 이미지 | `layout/regions.py`, `ir.Region` |
| | 타일 중복 병합 · OCR 줄 dedupe · Region 조립 · 미배정 흡수 · 읽기순서 · VLM |

### `--sizing`

| 값 | 동작 |
| --- | --- |
| `asis` | 기존 동작 재현. PDF 는 `structured`→200 DPI, `scan_like`/`hybrid`→내장 래스터 해상도(72~200). 이미지는 원본 픽셀. **비교 기준선** |
| `maxside` | 긴 변이 `--max-side`(기본 2500) 이하가 되도록. PDF 는 DPI 를 역산하고 이미지는 축소한다. **확대는 하지 않는다** |

> **주의 — 픽셀 수는 레이아웃 품질의 축이 아니다.**
> 레이아웃 모델은 입력이 몇 픽셀이든 **800×800 정사각으로 누른다**(`keep_ratio: false`).
> 그래서 12598×11024 를 보내든 2500×2187 을 보내든 **모델이 보는 그림은 같다.**
> 진짜 변수는 **가로세로 비율**이다.
>
> | 보내는 것 | 비율 | 800×800 에서 |
> | --- | --- | --- |
> | 1. 카드상품 전체 | 1.14 | 거의 정사각 — 왜곡 없음 |
> | 1. 카드상품 기존 타일 13 | **51** | 극단 왜곡 |
> | 올원e적금 전체 | 0.17 | 세로가 8배 납작 |
> | 올원e적금 기존 타일 | 0.7 | 양호 |
>
> `maxside` 의 효용은 **속도·전송량·OCR 잔글씨** 쪽이다. 레이아웃 품질을 좌우하는 것은
> "자르느냐, 자른다면 어떤 **모양**으로 자르느냐"다. 그래서 이 환경은 **안 자르는 것**을
> 기준선으로 두고, 비율 왜곡이 실제로 얼마나 해로운지부터 측정한다.

## 자르기 — 판단 기준은 비율 하나

기존 기준(`높이 > 4000px`)은 픽셀 기준이라 틀렸다. `1. 카드상품` 은 11024px 높이지만
비율이 1.14 라 자를 이유가 없었다. 레이아웃 모델이 800×800 정사각으로 누르므로 해로운
것은 **가로세로 비율**이다.

| 문서 | 왜곡(긴 변÷짧은 변) | 통짜 1회 호출 ① |
| --- | --- | --- |
| 2. 카드상품 | 1.00 | 36 ✅ |
| 1. 카드상품 | 1.14 | 25 ✅ |
| A4 3건 | 1.41 | 31 / 29 / 26 ✅ |
| 4. 카드상품 | 1.46 | 37 ✅ |
| 올원e적금 | 5.73 | 34 |
| NH002 | 8.49 | **2** ❌ 상단 73% 에서 박스 0개 |

1.46 과 5.73 사이가 비어 있어 그 사이 어디에 선을 그어도 8건은 같게 갈린다.
`--aspect-limit` 기본값 **2.0**. 정확한 경계는 미측정이다.

**자르는 위치와 조각 크기는 기존 로직 그대로** `nh_parser.ocr.bands.content_bands`,
span 1600px — 글자량 등분 + 빈 줄로 스냅 + 오버랩.

| | 기존 | ocr-lab |
| --- | --- | --- |
| **자를지 판단** | 높이 > 4000px | **긴 변÷짧은 변 > 2.0** ← 바뀐 것은 이것뿐 |
| 목표 조각 크기 | 1600px 고정 | 동일 |
| 오버랩 | 200px 고정 | 동일 (1600 의 15% → 상한 200) |
| 자르는 위치 | 글자 밀도 | 동일 |

### 정사각 조각은 시험했다가 되돌렸다

조각을 정사각(짧은 변)으로 만들면 800×800 에서 왜곡이 없어지니 나아질 것으로 봤는데,
**측정해 보니 차이가 없었다** (2026-09-18, `compare.py --a tiled --b span1600`).

| | 정사각 | 1600px 고정 |
| --- | --- | --- |
| 6개 통짜 문서 | 커버리지 완전 동일 (자르지 않으므로 당연) | |
| NH002 | 4곳 놓침 | **2곳 놓침** |
| 올원e적금 | **0곳 놓침** | 1곳 놓침 |
| **합계** | 영역 268개 중 **순증 1곳**. 방향도 문서마다 엇갈림 | |

세로로 긴 문서의 기존 조각도 이미 왜곡 1.43~2.22 로 나쁘지 않았다. 51:1 같은 극단은
**가로로 넓은 문서에서만** 벌어지던 일이고, 그건 "자를지 말지"를 비율로 판단하는 것으로
이미 해결된다. `--tile-span 0` 으로 정사각 모드를 재현할 수 있다.

`--dedupe` 를 주면 조각 경계 중복 박스를 합친다(기존과 같은 기준: 다른 조각 + 같은 라벨 +
가로 90% · 세로 50% 중첩). 기본은 꺼져 있어 **중복이 그대로 보인다** — 무엇이 겹쳤는지
먼저 눈으로 보라는 뜻이다. 조각별 원본 응답은 `raw/<문서>_p001_t00.json` 으로 따로 남는다.

## 실행

```powershell
# ← 노트북 창 1: spark-1118 터널 유지
ssh -N -L 18081:127.0.0.1:8081 spark-1118
```

```powershell
# ← 노트북 창 2 (저장소 루트)
uv run python experiments/ocr-lab/run.py --run-name asis `
  --input "samples/spark1118-sample" --sizing asis

uv run python experiments/ocr-lab/run.py --run-name max2500 `
  --input "samples/spark1118-sample" --sizing maxside --max-side 2500 `
  --compare-with asis

# 비율 2.0 을 넘는 입력만 자른다 (기본값)
uv run python experiments/ocr-lab/run.py --run-name tiled `
  --input "samples/spark1118-sample" --tiling auto --dedupe

# 두 실행을 영역 커버리지로 대조한다 (박스 개수가 아니라)
uv run python experiments/ocr-lab/compare.py --a asis --b tiled
```

## 전체 설정 매트릭스

`matrix.py`는 입력 처리 조건을 고정하고 PaddleX 요청값을 하나씩만 바꿔 모든 샘플을
순차 실행한다. 일반 비율 페이지는 통짜로 한 번 호출하고, 종횡비가 한계를 넘는 입력만
1600px 글자밀도 밴드로 분할한 뒤 원본 좌표 복원과 타일 중복 제거를 수행한다.

```powershell
# 레이아웃 설정: 서버 기준, table/region on-off, merge, threshold, unclip
uv run python experiments/ocr-lab/matrix.py `
  --name layout-20260918-v1 --phase layout

# 레이아웃 기준을 고른 뒤 글자 검출 크기·문턱 실험
uv run python experiments/ocr-lab/matrix.py `
  --name ocr-20260918-v1 --phase ocr
```

완료된 실행은 다시 시작할 때 자동으로 건너뛴다. `--force`를 주면 같은 이름을 다시
실행한다. 산출물은 `matrices/<name>/`의 `summary.md`, `summary.json`, `pages.csv`,
`tasks-review.json`, `tasks-parsing.json`, `tasks-unassigned.json`, `tasks-det.json`,
`tasks.json`, `labeling-config.xml`이다. 기본값은 사람이 계속 비교할 필요가 있는
`merge-union`, `merge-large` 두 설정만 Label Studio 파일에 넣는다. 서버 호출 결과와
전체 통계에는 다른 실험도 그대로 남는다.

영역 품질과 누락 원인을 함께 볼 때는 `tasks-review.json`, 영역 경계만 볼 때는
`tasks-parsing.json`, 원검출 진단에는 `tasks-det.json`, 모든 레이어를 함께 볼 때는
`tasks.json`을 가져온다. 같은 페이지에서 설정을 전환해 비교할 수 있다.

`--input` 은 파일 여러 개 또는 폴더(`pdf`/`png`/`jpg`/`hwp`/`hwpx`). PaddleX 요청값은
전부 플래그로 있고 기본은 `server`(= 그 키를 안 보냄 = 서버 YAML 값 사용)다.

> HWP 는 내장 이미지 자산을 페이지로 본다. 사내 파서 `document_processor` 가 필요하며,
> 현재 샘플에 HWP 가 없어 **실행 검증은 아직 못 했다.**

## 산출물

```text
experiments/ocr-lab/runs/<run-name>/
  run.json              보낸 요청 본문 · 페이지별 크기/비율/시간 · 박스 수 · triage 기록
  raw/<문서>_p001.json  PaddleX 응답 원본 (prunedResult 그대로)
  overlay/<문서>_p001_1det.jpg     검출기 원출력 박스
  overlay/<문서>_p001_2parsing.jpg 엔진 정리본 박스
  overlay/<문서>_p001_3ocr.jpg     전체 OCR 줄
  overlay/<문서>_p001_4unassigned.jpg parsing 영역 밖 OCR 줄
  tasks.json            Label Studio import
  labeling-config.xml   이번 실행에 나온 라벨로 만든 설정
```

전부 Git ignore 대상이다.

## Label Studio 에서 보기

페이지 PNG 는 기존 `experiments/layout-labeling/.local/media/` 에 `lab_<run-name>__` 접두어로
저장된다. 그 폴더가 이미 컨테이너에 마운트돼 있어 새 설정이 필요 없다.

```powershell
docker compose -f experiments/layout-labeling/docker-compose.yml up -d
```

1. 새 프로젝트를 만들고 **Labeling Setup → Custom template(Code)** 에
   `runs/<run-name>/labeling-config.xml` 내용을 붙여넣는다.
   (기존 PP 20클래스 설정을 쓰면 `vision_footnote` 처럼 목록 밖 라벨이 조용히 사라진다.)
2. **Settings → Cloud Storage → Add Source Storage → Local Files**,
   Absolute local path `/label-studio/files/pages`.
3. **Import** 에 `runs/<run-name>/tasks.json` 을 올린다.

개별 실행의 task에는 prediction이 4벌 들어간다.

| prediction | 무엇 |
| --- | --- |
| `<run> 1-det` | `layout_det_res` — 검출기 원출력. 확신도(score)가 붙어 온다 |
| `<run> 2-parsing` | `parsing_res_list` — 엔진이 읽기순서까지 정리한 목록. score 없음 |
| `<run> 3-ocr` | `overall_ocr_res` — 글자 검출기가 찾고 인식한 모든 OCR 줄 |
| `<run> 4-unassigned` | OCR 줄 면적의 50% 이상을 품는 parsing 영역이 없는 줄 |

1~3은 PaddleX 응답에서 꺼낸 값이다. 4는 PaddleX의 별도 결과가 아니라 실제 파서의
줄→영역 배정 기준과 같은 규칙으로 계산한 **우리 후처리 진단층**이다. 이 탭에서 보이는
글자는 "OCR도 못 읽은 글자"가 아니라 "OCR은 읽었지만 현재 영역 후보가 담지 못한 글자"다.

기존 실행 결과만으로 Label Studio 파일을 다시 만들 때는 서버를 호출하지 않는다.

```powershell
uv run python experiments/ocr-lab/matrix.py `
  --name layout-20260918-v1 --phase layout --artifacts-only `
  --label-studio-settings merge-union merge-large
```

현재 권장 import 파일은
`matrices/layout-20260918-v1/tasks-review.json`이다. 각 페이지에는 아래 여섯 prediction만
들어간다.

```text
merge-large · 2-parsing / 3-ocr / 4-unassigned
merge-union · 2-parsing / 3-ocr / 4-unassigned
```
