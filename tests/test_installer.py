from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(sum(message.startswith("WOULD_INSTALL ") for message in messages), 6)
        self.assertEqual(sum(message.startswith("RUNTIME_WOULD_SWITCH ") for message in messages), 1)
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
        runtime_root = self.home / install_agents.RUNTIME_SUBDIR
        self.assertTrue(runtime_root.is_symlink())
        self.assertIn(str(runtime_root / "scripts" / "router_mcp.py"), config)
        self.assertNotIn(str(runtime_root.resolve() / "scripts" / "router_mcp.py"), config)
        self.assertTrue((runtime_root / "hooks" / "router_hook.py").is_file())
        self.assertTrue((runtime_root / "scripts" / "run_agent.py").is_file())
        self.assertTrue((runtime_root / "assets" / "receipt.schema.json").is_file())
        self.assertTrue((runtime_root / "install" / "agent-definitions" / "router_scout.toml").is_file())
        self.assertTrue(any(message.startswith("RUNTIME_KEEP") for message in messages))
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

    def test_modified_stable_runtime_is_preserved(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        target = self.home / install_agents.RUNTIME_SUBDIR / "scripts" / "router_mcp.py"
        target.write_text("# user modification\n", encoding="utf-8")
        errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "# user modification\n")
        self.assertTrue(any("RUNTIME_CONFLICT" in message for message in messages))

    def test_unowned_runtime_conflict_blocks_install_before_config_or_agents(self):
        target = self.home / install_agents.RUNTIME_SUBDIR
        target.mkdir(parents=True)
        errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 1)
        self.assertTrue(any("RUNTIME_CONFLICT" in message for message in messages))
        self.assertTrue(target.is_dir())
        self.assertFalse((self.home / "config.toml").exists())
        self.assertFalse((self.home / "agents").exists())

    def test_release_rejects_intermediate_symlink_without_touching_target(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        release = self.home / install_agents.RUNTIME_RELEASES_SUBDIR / install_agents.runtime_release_id()
        release.mkdir(parents=True)
        (release / "scripts").symlink_to(outside, target_is_directory=True)
        errors, messages = install_agents.install(self.home, apply=True)
        self.assertGreater(errors, 0)
        self.assertTrue(any("unsafe release path component" in message or "symlink inside release" in message for message in messages))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((self.home / "config.toml").exists())

    def test_runtime_release_switch_is_atomic_and_keeps_previous_release(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        current = self.home / install_agents.RUNTIME_SUBDIR
        old_target = os.readlink(current)
        old_release = current.parent / old_target
        replacement = Path(self.temp.name) / "replacement.py"
        replacement.write_text("print('new release')\n", encoding="utf-8")
        with mock.patch(
            "install_agents.runtime_sources",
            return_value=[(Path("scripts/replacement.py"), replacement)],
        ):
            errors, _ = install_agents.install_runtime(
                self.home,
                apply=True,
                manifest=install_agents.load_manifest(self.home / "smart-router" / "installed.json"),
            )
        self.assertEqual(errors, 0)
        self.assertNotEqual(os.readlink(current), old_target)
        self.assertTrue((current / "scripts" / "replacement.py").is_file())
        self.assertTrue(old_release.is_dir())

    def test_concurrent_same_release_publish_converges_cleanly(self):
        def concurrent_publish(staging, release):
            shutil.copytree(staging, release)
            raise FileExistsError(release)

        with mock.patch("install_agents.os.rename", side_effect=concurrent_publish):
            errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0, messages)
        self.assertTrue((self.home / install_agents.RUNTIME_SUBDIR).is_symlink())
        releases = self.home / install_agents.RUNTIME_RELEASES_SUBDIR
        self.assertFalse(any(path.name.startswith(".staging-") for path in releases.iterdir()))

    def test_failed_runtime_copy_cleans_its_staging_directory(self):
        with mock.patch("install_agents.atomic_copy", side_effect=OSError("injected copy failure")):
            with self.assertRaisesRegex(OSError, "injected copy failure"):
                install_agents.install(self.home, apply=True)
        releases = self.home / install_agents.RUNTIME_RELEASES_SUBDIR
        self.assertTrue(releases.is_dir())
        self.assertFalse(any(path.name.startswith(".staging-") for path in releases.iterdir()))

    def test_owned_unchanged_old_version_is_upgraded_atomically(self):
        target = self.home / "agents" / "router_worker.toml"
        target.parent.mkdir(parents=True)
        target.write_text('name = "router_worker"\ndescription = "old"\n', encoding="utf-8")
        manifest = {
            "manifest_version": 1,
            "files": {
                "router_worker.toml": {
                    "sha256": install_agents.digest(target),
                    "status": "active",
                }
            },
        }
        manifest_path = self.home / "smart-router" / "installed.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        replacement = Path(self.temp.name) / "router_worker.toml"
        replacement.write_text(
            'name = "router_worker"\ndescription = "new"\nmodel = "gpt-5.6-terra"\n',
            encoding="utf-8",
        )
        with mock.patch("install_agents.source_files", return_value=[replacement]):
            errors, messages = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        self.assertIn("description = \"new\"", target.read_text(encoding="utf-8"))
        self.assertTrue(any(message.startswith("UPDATE") for message in messages))
        updated = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["files"]["router_worker.toml"]["sha256"], install_agents.digest(replacement))

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
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 6)
        self.assertTrue((self.home / "config.toml").exists())

    def test_runtime_conflict_aborts_uninstall_before_agents_or_config_change(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        runtime_file = self.home / install_agents.RUNTIME_SUBDIR / "scripts" / "router_mcp.py"
        runtime_file.write_text("# modified runtime\n", encoding="utf-8")
        errors, messages = install_agents.uninstall(self.home)
        self.assertGreater(errors, 0)
        self.assertTrue(any("RUNTIME_PRESERVE" in message for message in messages))
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 6)
        self.assertIn("[mcp_servers.smart_router]", (self.home / "config.toml").read_text(encoding="utf-8"))

    def test_uninstall_rejects_release_id_path_escape(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        manifest_path = self.home / "smart-router" / "installed.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime_release"]["id"] = "../outside"
        manifest["runtime_release"]["link_target"] = "runtime-releases/../outside"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        errors, messages = install_agents.uninstall(self.home)
        self.assertGreater(errors, 0)
        self.assertTrue(any("invalid runtime release manifest" in message for message in messages))
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 6)

    def test_uninstall_rejects_release_parent_symlink(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        releases = self.home / install_agents.RUNTIME_RELEASES_SUBDIR
        outside = Path(self.temp.name) / "outside-releases"
        releases.rename(outside)
        releases.symlink_to(outside, target_is_directory=True)
        errors, messages = install_agents.uninstall(self.home)
        self.assertGreater(errors, 0)
        self.assertTrue(any("releases root" in message for message in messages))
        self.assertEqual(len(list((self.home / "agents").glob("router_*.toml"))), 6)

    def test_uninstall_rejects_legacy_intermediate_symlink(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        outside = Path(self.temp.name) / "outside-legacy"
        outside.mkdir()
        external = outside / "router_mcp.py"
        external.write_text("# external\n", encoding="utf-8")
        legacy = self.home / install_agents.LEGACY_RUNTIME_SUBDIR
        legacy.mkdir()
        (legacy / "scripts").symlink_to(outside, target_is_directory=True)
        manifest_path = self.home / "smart-router" / "installed.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["legacy_runtime_files"] = {
            "scripts/router_mcp.py": {"sha256": install_agents.digest(external)}
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        errors, messages = install_agents.uninstall(self.home)
        self.assertGreater(errors, 0)
        self.assertTrue(any("unsafe legacy path component" in message for message in messages))
        self.assertEqual(external.read_text(encoding="utf-8"), "# external\n")

    def test_uninstall_rejects_agents_root_symlink(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        agents = self.home / "agents"
        outside = Path(self.temp.name) / "outside-agents"
        agents.rename(outside)
        agents.symlink_to(outside, target_is_directory=True)
        errors, messages = install_agents.uninstall(self.home)
        self.assertGreater(errors, 0)
        self.assertTrue(any("agents root" in message for message in messages))
        self.assertEqual(len(list(outside.glob("router_*.toml"))), 6)

    def test_agent_manifest_names_cannot_escape_managed_roots(self):
        for index, malicious_name in enumerate(("../external.toml", ".", "/tmp/external.toml")):
            with self.subTest(name=malicious_name):
                home = Path(self.temp.name) / f"escape-home-{index}" / ".codex"
                errors, _ = install_agents.install(home, apply=True)
                self.assertEqual(errors, 0)
                external = home / "external.toml"
                external.write_text("external\n", encoding="utf-8")
                manifest_path = home / "smart-router" / "installed.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["files"][malicious_name] = {
                    "sha256": install_agents.digest(external),
                    "status": "active",
                }
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                errors, messages = install_agents.uninstall(home)
                self.assertGreater(errors, 0)
                self.assertTrue(any("invalid managed agent manifest entry" in message for message in messages))
                self.assertEqual(external.read_text(encoding="utf-8"), "external\n")

    def test_disable_rejects_agent_manifest_path_escape(self):
        errors, _ = install_agents.install(self.home, apply=True)
        self.assertEqual(errors, 0)
        external = self.home / "external.toml"
        external.write_text("external\n", encoding="utf-8")
        manifest_path = self.home / "smart-router" / "installed.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["../external.toml"] = {
            "sha256": install_agents.digest(external),
            "status": "active",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        errors, messages = install_agents.disable(self.home)
        self.assertGreater(errors, 0)
        self.assertTrue(any("invalid managed agent manifest entry" in message for message in messages))
        self.assertEqual(external.read_text(encoding="utf-8"), "external\n")

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
                self.assertIn("receipt-v2 JSON object", raw)


if __name__ == "__main__":
    unittest.main()
