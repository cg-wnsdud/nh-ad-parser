import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "experiments" / "parser-v2" / "config.py"
SPEC = importlib.util.spec_from_file_location("parser_v2_config", MODULE_PATH)
assert SPEC and SPEC.loader
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


def test_paddlex_options_are_owned_by_server_yaml():
    profile = config.Profile()

    assert profile.request_payload == {"fileType": 1}
    assert profile.manifest()["paddlex_options_source"] == "server_pipeline_yaml"
