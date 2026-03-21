from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
import warnings


def _load_opencode_config_module():
    package = types.ModuleType("mcp_tool_router")
    package.__path__ = []
    package = sys.modules.setdefault("mcp_tool_router", package)

    module_dir = Path(__file__).resolve().parents[1] / "mcp_tool_router"
    for name in ["jsonc", "opencode_config"]:
        module_path = module_dir / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"mcp_tool_router.{name}", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to load {name} module for tests")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        setattr(package, name, module)

    return sys.modules["mcp_tool_router.opencode_config"]


_opencode_config = _load_opencode_config_module()


class OpenCodeConfigJsoncTests(unittest.TestCase):
    def test_apply_router_config_preserves_jsonc_path_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "opencode.jsonc"
            original = """
            {
              // jsonc source should be accepted
              "mcp": {
                "router": {
                  "enabled": false,
                },
              },
            }
            """
            config_path.write_text(original, encoding="utf-8")

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                payload = _opencode_config.apply_router_config(str(config_path))

            self.assertNotIn("router", payload["mcp"])
            self.assertTrue(config_path.exists())
            self.assertEqual(config_path.with_suffix(".json").exists(), False)
            self.assertEqual(
                config_path.with_suffix(config_path.suffix + ".bak").read_text(encoding="utf-8"),
                original,
            )

            written_payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIn("context7", written_payload["mcp"])
            self.assertTrue(written_payload["mcp"]["context7"]["enabled"])


if __name__ == "__main__":
    unittest.main()
