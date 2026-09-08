"""C안: CUSTOMIZE 훅에 실제로 무엇이 전달되는지만 기록하는 비운영 프로브.

이 코드는 OCR이나 DLA를 수행하지 않는다. ``extract()`` 뒤에 플랫폼 처리가 이어지는지
확인하기 위한 증거 파일만 남긴다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.custom_extension.custom_extension import CustomExtension


class BaseCustomExtension(CustomExtension):
    async def extract(
        self,
        input_path: str,
        output_path: str,
        digitize_config: dict = {},
    ):
        source = Path(input_path)
        destination = Path(output_path)
        destination.mkdir(parents=True, exist_ok=True)
        evidence = {
            "purpose": "CUSTOMIZE hook contract probe; not a parser result",
            "called_at_utc": datetime.now(timezone.utc).isoformat(),
            "input_path": str(source),
            "input_exists": source.exists(),
            "input_size": source.stat().st_size if source.is_file() else None,
            "output_path": str(destination),
            "digitize_config": digitize_config,
            "warning": "No DLA/OCR was called by this probe.",
        }
        (destination / "cginside_customize_probe.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    async def transform(self, input_file_path, dla_result_path, exec_args, stop_flag):
        # 이 후보의 목적은 CUSTOMIZE 분기 관찰뿐이다.
        return None

    def get_custom_routers(self):
        return []
