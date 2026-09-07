# nh-ad-parser

광고물(PDF·이미지·HWP)을 넣으면 **글자·좌표·영역**이 담긴 JSON 하나가 나온다.

```
입력 ──► 판별 ──► 렌더/분할 ──► OCR·레이아웃 ──► 영역 조립 ──► parse JSON
        triage    canvas        PaddleX          regions
                  tiling        PP-StructureV3   cards
                  bands                          gap
```

`nh-ad-review-poc` 에서 **파싱 계층만** 떼어내 구조를 다시 잡은 저장소다.
실험 산출물·일회성 도구·미확정 코드는 따라오지 않았다. 이관 근거와 경계는
[docs/이관-기록.md](docs/이관-기록.md).

## 빠른 시작

```bash
uv sync
cp .env.example .env      # PADDLEX_URL / GEMMA_URL / GEMMA_MODEL 채우기
uv run python tools/parse.py --input <파일 또는 폴더> --out out/
```

```
▶ [예금성상품-적금] 올원e적금.png
  쪽 1 / 영역 38 / 줄 73 / 17초  → out\parse\[예금성상품-적금] 올원e적금.json
```

PaddleX·Gemma 는 사내 서버에 있고 **WireGuard VPN 이 연결돼 있어야** 응답한다.
엔드포인트 설정을 확인하려면 `.env` 의 `GEMMA_URL` 을 `/models` 로 바꿔 GET 해 본다
(모델명 접두어가 바뀌면 VLM 호출이 전부 400 으로 죽는데 OCR 은 멀쩡히 돌아
산출물이 그럴듯하게 나온다 — 전례 있음).

## 산출물 구조

```jsonc
{
  "doc_id": "...", "source_file": "...", "file_type": "image",
  "product_group": "예금성", "ad_type": "상세페이지",   // 파일명·내용 기반 분류
  "pages": [{
    "page_no": 1, "canvas_w": 1122, "canvas_h": 6429, "dpi": 200,
    "parse_route": "ocr",          // ocr | digital | hybrid
    "parse_status": "ok",
    "regions": [{
      "region_id": "p1_r002",
      "bbox": [65, 349, 220, 402],
      "label": "text",             // PP-DocLayout_plus-L 의 20클래스
      "layout_score": 0.98,
      "role": "본문",              // 규칙 기반 — 아래 "범위" 참고
      "card_no": null,             // 카드형 광고에서 몇 번째 카드인지
      "table": null,               // 표면 행·열·셀 좌표
      "lines": [{
        "text": "연이자율",
        "bbox": [65, 349, 220, 402],
        "confidence": 0.998,
        "source": "ocr",           // ocr | digital | hwp
        "style": null              // pt·색·굵기 (디지털/HWP 입력일 때)
      }]
    }],
    "unassigned_lines": []         // 어느 영역에도 못 붙은 줄. 0 이어야 정상
  }]
}
```

**모든 글자는 어딘가에 남는다.** 영역에 못 붙은 줄도 `unassigned_lines` 로 싣는다.
줄이 사라지면 그 자체가 결함이다.

## 폴더 구조

| 경로 | 하는 일 |
| --- | --- |
| `src/nh_parser/config.py` | 모든 설정 한 곳. 임계치마다 왜 그 값인지 실측 근거가 주석에 있다 |
| `src/nh_parser/ir.py` | 데이터 모델 (`AdDocument` / `AdPage` / `Region` / `Line`) |
| `src/nh_parser/ingest/` | 입력 적재 — `triage`(형식·텍스트레이어 판별) `canvas`(렌더) `hwp` `assets` `text_style` |
| `src/nh_parser/ocr/` | `paddlex`(PP-StructureV3 호출) `tiling`(타일 분할) `bands`(글자밀도 밴드) |
| `src/nh_parser/layout/` | `regions`(줄→영역 배정) `cards`(카드 분할) `gap`(수직 갭 분리) |
| `src/nh_parser/vlm/` | `client`(Gemma) `cache` `direct` `view` `field_judge` `reading`(판독 교차검증) |
| `src/nh_parser/pipeline.py` | 위를 순서대로 엮는다. 진입점은 `process_file(path)` |
| `tools/parse.py` | CLI |
| `tests/` | 176 통과 / 37 건너뜀(샘플 PDF 필요) |

## 범위 — 여기 없는 것

이 저장소는 **"무엇이 어디에 적혀 있나"까지**다. "그래서 규정에 맞나"는 하지 않는다.

| 없는 것 | 어디 있(었)나 |
| --- | --- |
| 템플릿 판정·항목 라벨링 | `nh-ad-review-poc` 의 `ad_template.py` |
| 심의 근거 JSON·검수 화면 | 같은 곳 `ad_export.py`, `ad_review_*.py` |
| 필드 추출·정규화 내보내기 | `extract.py`, `normalized_export.py`, `kl_export.py` |
| VLM 역할 판정 | `vlm_judge.py` |

**`region.role` 은 규칙 기반 폴백값이다.** 원래 설계는 VLM(`vlm_judge`)이 이걸
덮어쓰는 것이고, 그 모듈은 라벨링 계층에 있어 여기 없다. 규칙 결과를 최종 역할로
믿으면 안 된다.

라벨링을 여기로 가져오지 않은 이유는 2026-09-07 실험에서 **라벨 설명 방식이
미확정으로 판명**됐기 때문이다(설명을 넣으니 정확도가 23/33 → 18/33 로 떨어졌다).
확정된 뒤에 옮기는 게 맞다.

## HWP 입력

HWP/HWPX 는 사내 `document-processor` 가 있어야 한다. 사설 저장소라 `pyproject.toml`
에 못 박지 않았다 — 넣으면 접근 권한 없는 곳에서 `uv sync` 자체가 실패한다.
`ingest/hwp.py` 가 함수 안에서 지연 import 하므로 없으면 **HWP 경로만** 막히고
PDF·이미지는 정상 동작한다.

```bash
uv pip install "document-processor @ git+ssh://git@github.com/CGINSIDE-ROOKIES/document-processor.git"
```

## 알려진 한계

- **한국어 OCR 정확도 천장 88%.** 서버가 `korean_PP-OCRv5_mobile_rec` 를 쓰는데
  PP-OCRv5 에 한국어 server 인식 모델이 아예 없다. 파라미터로 못 넘는다.
- **특수문자가 깨진다.** 실측: `① → 1`, `「」 → []`, `⑦번동의서 → 번동의서`,
  `동의 시¹ → 동의 시1`. 마지막 것은 각주 참조와 값을 구별 못 하게 만든다.
- **한 덩어리가 두 영역으로 갈리는 경우가 남아 있다.** 실측(올원e적금 36블록 중 3건):
  숫자와 그 조건, 표제와 본문, 목록 ①②와 ③이 갈렸다.
- **VLM 호출은 같은 입력에도 흔들린다.** 서버가 fp8 KV 캐시·prefix caching·chunked
  prefill·투기 디코딩을 켠 채로 떠 있어 `temperature=0` 으로도 못 막는다.
  카드 분할·밴드 판독처럼 VLM 을 쓰는 단계는 재실행하면 결과가 달라질 수 있다.
- 표로 검출되지 않는 2열 항목표(`대출대상 | 내용`)는 짝을 못 맞춘다.
