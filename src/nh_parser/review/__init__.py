"""광고 템플릿 선택·영역 라벨링·심의 입력 계약."""

from .export import build_evidence, build_review_input
from .labeling import label_regions
from .resolution import resolve_template

__all__ = ["build_evidence", "build_review_input", "label_regions", "resolve_template"]
