from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import install_agents


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / ".codex"

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_has_no_side_effects(self):
        errors, messages = install_agents.install(self.home, apply=False)
        self.assertEqual(errors, 0)
        self.assertEqual(sum("WOULD_INSTALL" in message for message in messages), 6)
        self.assertTrue(any("[agents]" in message for message in messages))
        self.assertFalse(self.home.exists())

    def test_install_is_idempotent(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        self.assertEqual(sum(message.startswith("KEEP") for message in messages), 6)
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 6)
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(config.count("[agents]"), 1)
        self.assertEqual(config.splitlines().count(install_agents.CONFIG_BEGIN), 1)
        self.assertEqual(config.splitlines().count(install_agents.MCP_BEGIN), 1)
        self.assertIn("[mcp_servers.smart_router]", config)
        self.assertIn(str((ROOT / "scripts" / "router_mcp.py").resolve()), config)
        for source in install_agents.source_files():
            self.assertIn(f"{source.stem} = {{", config)
            self.assertIn(str((self.home / "agents" / source.name).resolve()), config)

    def test_existing_agents_section_is_extended_without_duplication(self):
        self.home.mkdir(parents=True)
        (self.home / "config.toml").write_text("[agents]\nenabled = true\n\n[features]\nhooks = true\n", encoding="utf-8")
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(config.count("[agents]"), 1)
        self.assertIn("max_concurrent_threads_per_session = 3", config)
        self.assertIn("router_scout = {", config)
        self.assertIn("[mcp_servers.smart_router]", config)

    def test_incompatible_agents_config_fails_closed(self):
        self.home.mkdir(parents=True)
        config = self.home / "config.toml"
        config.write_text("[agents]\nenabled = false\n", encoding="utf-8")
        errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 1)
        self.assertTrue(any("CONFIG_CONFLICT" in message for message in messages))
        self.assertFalse((self.home / "agents").exists())
        self.assertEqual(config.read_text(encoding="utf-8"), "[agents]\nenabled = false\n")

    def test_conflict_is_preserved(self):
        target = self.home / "agents" / "router_scout.toml"
        target.parent.mkdir(parents=True)
        target.write_text("user content\n", encoding="utf-8")
        errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "user content\n")
        self.assertTrue(any("CONFLICT" in message for message in messages))

    def test_disable_and_enable_are_recoverable(self):
        install_agents.install(self.home, apply=True)
        errors, _ = install_agents.disable(self.home)
        self.assertEqual(errors, 0)
        self.assertTrue((self.home / "smart-router" / "DISABLED").exists())
        self.assertFalse(any((self.home / "agents").glob("router_*.toml")))
        self.assertEqual(len(list((self.home / "smart-router" / "disabled").glob("router_*.toml"))), 6)
        errors, _ = install_agents.enable(self.home)
        self.assertEqual(errors, 0)
        self.assertFalse((self.home / "smart-router" / "DISABLED").exists())
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 6)

    def test_uninstall_preserves_modified_owned_file(self):
        install_agents.install(self.home, apply=True)
        target = self.home / "agents" / "router_worker.toml"
        target.write_text(target.read_text(encoding="utf-8") + "# user edit\n", encoding="utf-8")
        errors, messages = install_agents.uninstall(self.home)
        self.assertEqual(errors, 1)
        self.assertTrue(target.exists())
        self.assertTrue(any("PRESERVE" in message for message in messages))
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 1)

    def test_agent_definitions_are_valid_and_pinned(self):
        expected_models = {
            "router_worker": "gpt-5.6-terra",
            "router_reviewer": "gpt-5.6-terra",
            "router_scout": "gpt-5.6-luna",
            "router_monitor": "gpt-5.6-luna",
            "router_tester": "gpt-5.6-luna",
            "router_docs": "gpt-5.6-luna",
        }
        for path in install_agents.source_files():
            with self.subTest(path=path.name):
                raw = path.read_text(encoding="utf-8")
                fields = dict(re.findall(r'^(name|model|sandbox_mode) = "([^"]+)"$', raw, re.M))
                self.assertEqual(fields["model"], expected_models[fields["name"]])
                self.assertIn(fields["sandbox_mode"], {"read-only", "workspace-write"})
                self.assertIn('developer_instructions = """', raw)
                self.assertIn("Return exactly one JSON object", raw)


if __name__ == "__main__":
    unittest.main()
