from __future__ import annotations

import json
import io
import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_agent
import router_mcp


def valid_receipt():
    return {
        "status": "completed",
        "summary": "done",
        "findings": [],
        "evidence": [],
        "changed_files": [],
        "validation": ["ok"],
        "remaining_risks": [],
        "needs_escalation": False,
        "recommended_next_action": "integrate",
    }


class WrapperTests(unittest.TestCase):
    def test_commands_pin_role_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            for role, (model, effort, sandbox) in run_agent.ROLE_SETTINGS.items():
                with self.subTest(role=role):
                    command = run_agent.build_command(role, Path(tmp) / "out.json")
                    self.assertIn(model, command)
                    self.assertIn(f'model_reasoning_effort="{effort}"', command)
                    self.assertIn(f'sandbox_mode="{sandbox}"', command)
                    self.assertIn("--ephemeral", command)
                    self.assertIn("hooks", command)

    def test_high_risk_is_rejected_before_subprocess(self):
        with mock.patch("run_agent.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "high-risk"):
                run_agent.run_task("router_worker", "部署生产数据库迁移")
            run.assert_not_called()

    def test_writer_without_positive_authorization_is_rejected_before_subprocess(self):
        with mock.patch("run_agent.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "explicit positive write authorization"):
                run_agent.run_task(
                    "router_worker",
                    "只读盘点相关文件，不要实现、不要修复、不要改代码",
                )
            run.assert_not_called()

    def test_bounded_file_creation_is_allowed_for_worker(self):
        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task(
                "router_worker",
                "在当前工作区根目录仅新建 smart-router-smoke.txt，并写入 OK 后跟换行",
            )
        self.assertEqual(receipt["_router_meta"]["model"], "gpt-5.6-terra")

    def test_workspace_lock_rejects_concurrent_writer_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with run_agent.workspace_writer_lock(workspace):
                with mock.patch("run_agent.subprocess.run") as run:
                    with self.assertRaisesRegex(RuntimeError, "another writable routing task"):
                        run_agent.run_task(
                            "router_worker",
                            "新建 concurrent.txt 并写入 concurrent",
                            workspace=workspace,
                        )
                    run.assert_not_called()

    def test_valid_receipt_gets_executor_metadata(self):
        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task("router_scout", "搜索仓库中的测试文件")
        self.assertEqual(receipt["_router_meta"]["model"], "gpt-5.6-luna")
        self.assertEqual(receipt["_router_meta"]["executor"], "codex-exec-wrapper")

    def test_mcp_tool_schema_has_all_roles(self):
        tool = router_mcp.tool_definition()
        self.assertEqual(tool["name"], "route_task")
        roles = tool["inputSchema"]["properties"]["role"]["enum"]
        self.assertEqual(set(roles), set(run_agent.ROLE_SETTINGS))

    def test_mcp_version_comes_from_manifest(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(router_mcp.plugin_version(), manifest["version"])

    def test_mcp_marks_blocked_receipt_as_error(self):
        blocked = {**valid_receipt(), "status": "blocked"}
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "route_task", "arguments": {"role": "router_scout", "task": "搜索文件"}},
        }
        stream = io.StringIO()
        with mock.patch("router_mcp.routing_enabled", return_value=True), mock.patch(
            "router_mcp.run_task", return_value=blocked
        ), redirect_stdout(stream):
            router_mcp.handle(request)
        response = json.loads(stream.getvalue())
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], "blocked")

    def test_mcp_respects_global_plugin_and_park_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "config.toml"
            config.write_text('[plugins."codex-smart-router@personal"]\nenabled = true\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertTrue(router_mcp.routing_enabled())
                (home / "smart-router").mkdir()
                (home / "smart-router" / "DISABLED").write_text("parked\n", encoding="utf-8")
                self.assertFalse(router_mcp.routing_enabled())
                (home / "smart-router" / "DISABLED").unlink()
                config.write_text('[plugins."codex-smart-router@personal"]\nenabled = false\n', encoding="utf-8")
                self.assertFalse(router_mcp.routing_enabled())


if __name__ == "__main__":
    unittest.main()
