import json
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import router_core  # noqa: E402


class EconomicsMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = ROOT / "tests" / "fixtures" / "routing-economics-v042.json"
        cls.cases = json.loads(fixture.read_text(encoding="utf-8"))

    def test_preregistered_four_project_routing_matrix(self):
        self.assertEqual(len(self.cases), 20)
        for case in self.cases:
            with self.subTest(project=case["project"], name=case["name"]):
                decision = router_core.classify(
                    case["prompt"],
                    economics=True,
                    execution_profile=case.get("execution_profile", "STABLE"),
                    light_profile=case.get("light_profile", "LUNA_STABLE"),
                )
                self.assertEqual(decision["decision"], case["decision"], decision)
                self.assertEqual(decision.get("role"), case["role"], decision)
                self.assertEqual(decision["risk"], case["risk"], decision)

    def test_optional_real_project_paths_exist(self):
        workspace = os.environ.get("SMART_ROUTER_EVAL_WORKSPACE_ROOT")
        if not workspace:
            self.skipTest("set SMART_ROUTER_EVAL_WORKSPACE_ROOT for local real-project validation")
        workspace_root = Path(workspace).expanduser().resolve()
        for case in self.cases:
            project_root = workspace_root / case["project"]
            with self.subTest(project=case["project"], name=case["name"]):
                self.assertTrue(project_root.is_dir(), project_root)
                for relative in case["paths"]:
                    self.assertTrue((project_root / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
