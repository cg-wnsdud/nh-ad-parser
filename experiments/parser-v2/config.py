"""parser-v2의 재현 가능한 실행 프로필."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Profile:
    paddlex_url: str = "http://127.0.0.1:18081/layout-parsing"
    timeout: int = 300
    aspect_limit: float = 2.0
    tile_span: int = 1600
    encode: str = "jpeg"

    @classmethod
    def from_env(cls) -> "Profile":
        return cls(
            paddlex_url=os.environ.get("PADDLEX_URL", cls.paddlex_url),
            timeout=int(os.environ.get("PADDLEX_TIMEOUT", cls.timeout)),
            aspect_limit=float(os.environ.get("PARSER_V2_ASPECT_LIMIT", cls.aspect_limit)),
            tile_span=int(os.environ.get("PARSER_V2_TILE_SPAN", cls.tile_span)),
            encode=os.environ.get("PARSER_V2_ENCODE", cls.encode),
        )

    @property
    def request_payload(self) -> dict:
        # threshold/unclip/textDet 값은 일부러 보내지 않아 spark-1118 YAML을 사용한다.
        return {
            "fileType": 1,
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useFormulaRecognition": False,
            "useTextlineOrientation": False,
            "useTableRecognition": False,
            "layoutMergeBboxesMode": "large",
        }

    def manifest(self) -> dict:
        return {**asdict(self), "request_payload": self.request_payload}
