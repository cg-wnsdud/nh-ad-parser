# AGENTS.md

이 저장소에서 입력을 파싱해 테스트할 때는 반드시
[docs/테스트-실행-지침.md](docs/테스트-실행-지침.md) 를 따른다.

요약:

```bash
uv run python tools/parse.py --input <파일 또는 폴더> --out out/<YYYY-MM-DD>/<run-label> --preview
```

- `out/` 아래는 항상 `out/<날짜>/<라벨>/` 형태로 정리한다 (날짜는 `YYYY-MM-DD`,
  라벨은 무엇을 테스트했는지 알 수 있는 kebab-case).
- `--preview` 를 켜서 쪽 이미지도 같이 남긴다.
- 실행 후 "N/N 건 성공"과 "미배정줄" 유무를 확인한다.

세부 규칙·예시·흔한 실수는 위 문서 참고. 파서 자체의 구조·산출물 스키마·알려진
한계는 [README.md](README.md) 참고.
