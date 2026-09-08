"""B안: 농협 DLA 완료 뒤 현재 파이프라인 후반부를 실행하는 구현 후보.

주의: ``CustomExtension``의 실제 import 경로와 결과 저장 위치는 가이드에 없으므로
AgileSoDA 제공 원본 템플릿에서 확인한 뒤 확정해야 한다.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.custom_extension.custom_extension import CustomExtension
from nh_parser.integrations.etlwithllm import build_artifacts_from_dla
from nh_parser.ocr.etlwithllm import load_default_document


class BaseCustomExtension(CustomExtension):
    """가이드 p.53~54의 프로젝트별 구현체 후보."""

    async def extract(
        self,
        input_path: str,
        output_path: str,
        digitize_config: dict = {},
    ):
        raise RuntimeError(
            "CUSTOMIZE는 가이드의 etl_service.py 분기에서 dla_task()를 호출하지 않습니다. "
            "전처리 후 DLA 연결 계약이 확인되기 전에는 이 후보의 extract를 사용할 수 없습니다."
        )

    async def transform(
        self,
        input_file_path: Path,
        dla_result_path: Path,
        exec_args,
        stop_flag,
    ):
        # EtlStopFlag의 조회 API는 가이드에 없어 추측 구현하지 않는다.
        # 실제 SDK/소스를 받으면 페이지 또는 VLM 호출 단위의 취소 검사를 추가한다.
        document = load_default_document(
            Path(dla_result_path), source_name=Path(input_file_path).name,
        )
        configured = os.getenv("CGINSIDE_ETL_OUTPUT_DIR")
        dla_path = Path(dla_result_path)
        default_base = dla_path if dla_path.is_dir() else dla_path.parent
        output_dir = (
            Path(configured)
            if configured
            else default_base / "cginside-review"
        )
        build_artifacts_from_dla(
            Path(input_file_path), document, output_dir, preview=True,
        )

    def get_custom_routers(self):
        # 주 파이프라인은 transform 훅으로 충분하다. 불필요한 API를 열지 않는다.
        return []
