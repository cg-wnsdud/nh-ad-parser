"""Knowledge Lake/AWX Custom Parser 선택적 API 계층.

FastAPI는 ``kl-api`` extra에만 있으므로 패키지 최상위 import 시 웹 의존성을
강제하지 않는다. 서버 진입점은 ``nh_parser.kl_api.app:create_app``이다.
"""

__all__: list[str] = []
