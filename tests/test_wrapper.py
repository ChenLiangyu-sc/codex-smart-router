from __future__ import annotations

import json
import io
import subprocess
import os
import re
import sys
import tempfile
import threading
import unittest
import datetime as dt
from pathlib import Path
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_agent
import router_mcp
import router_core
import provider_policy
import local_provider


def valid_receipt(prompt=None):
    receipt = {
        "schema_version": 2,
        "objective_id": "0" * 64,
        "status": "completed",
        "summary": "done",
        "findings": [],
        "evidence": [],
        "evidence_manifest": [],
        "inconsistencies": [],
        "coverage": {"mode": "targeted", "checked": 1, "total": 1},
        "parent_verification": ["sample one result"],
        "changed_files": [],
        "validation": ["ok"],
        "remaining_risks": [],
        "needs_escalation": False,
        "recommended_next_action": "integrate",
    }
    if isinstance(prompt, str):
        match = re.search(r"Receipt objective_id \(copy exactly\): ([0-9a-f]{64})", prompt)
        if match:
            receipt["objective_id"] = match.group(1)
    return receipt


class WrapperTests(unittest.TestCase):
    def local_config(self, home, *, env_key="LOCAL_MODEL_API_KEY"):
        config = local_provider.LocalProviderConfig(
            provider_id="local_text_test",
            display_name="Local Text Test",
            base_url="http://127.0.0.1:8000/v1",
            model="deepseek-v4-flash",
            env_key=env_key,
        )
        local_provider.write_config(config, home)
        return config

    def test_commands_pin_role_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            for role, (model, effort, sandbox) in run_agent.ROLE_SETTINGS.items():
                with self.subTest(role=role):
                    command = run_agent.build_command(role, Path(tmp) / "out.json")
                    self.assertIn(model, command)
                    self.assertIn(f'model_reasoning_effort="{effort}"', command)
                    self.assertIn(f'sandbox_mode="{sandbox}"', command)
                    self.assertIn("--ephemeral", command)
                    for feature in (
                        "hooks",
                        "multi_agent",
                        "plugins",
                        "remote_plugin",
                        "apps",
                        "recommended_plugins",
                        "skill_search",
                        "skill_mcp_dependency_install",
                        "workspace_dependencies",
                    ):
                        self.assertIn(feature, command)

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
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
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
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task("router_scout", "搜索仓库中的测试文件")
        self.assertEqual(receipt["_router_meta"]["model"], "gpt-5.6-luna")
        self.assertEqual(receipt["_router_meta"]["executor"], "luna_scout")
        self.assertEqual(receipt["_router_meta"]["provider"], "openai")

    def test_usage_and_duration_are_recorded_without_child_content(self):
        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            stdout = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 120, "cached_input_tokens": 80, "output_tokens": 25},
                }
            )
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task("router_scout", "搜索仓库中的测试文件")
        self.assertEqual(
            receipt["_router_meta"]["usage"],
            {"input_tokens": 120, "cached_input_tokens": 80, "output_tokens": 25},
        )
        self.assertGreaterEqual(receipt["_router_meta"]["duration_ms"], 0)

    def test_flat_provider_objects_are_normalized_without_another_model_call(self):
        receipt = valid_receipt()
        receipt["findings"] = [{"severity": "high", "finding": "bounded defect"}]
        receipt["evidence"] = [{"path": "a.py", "line": 7}]
        raw, fields = run_agent._normalize_receipt_text(json.dumps(receipt))
        valid, errors, normalized = run_agent.validate_receipt(raw)
        self.assertTrue(valid, errors)
        self.assertEqual(fields, ["findings", "evidence"])
        self.assertIn("severity=high", normalized["findings"][0])

    def test_nested_provider_objects_remain_invalid(self):
        receipt = valid_receipt()
        receipt["findings"] = [{"finding": {"nested": "not accepted"}}]
        raw, fields = run_agent._normalize_receipt_text(json.dumps(receipt))
        valid, _, _ = run_agent.validate_receipt(raw)
        self.assertFalse(valid)
        self.assertEqual(fields, [])

    def test_nonfinite_provider_values_are_not_normalized(self):
        for value, marker in ((float("nan"), "NaN"), (float("inf"), "Infinity"), (-float("inf"), "-Infinity")):
            with self.subTest(marker=marker):
                receipt = valid_receipt()
                receipt["findings"] = [{"score": value}]
                raw, fields = run_agent._normalize_receipt_text(json.dumps(receipt))
                self.assertEqual(fields, [])
                self.assertIn(marker, raw)

    def test_timeout_partial_usage_is_preserved(self):
        partial = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 37, "cached_input_tokens": 11, "output_tokens": 5},
            }
        )
        timeout = subprocess.TimeoutExpired(["codex"], 1, output=partial)
        with mock.patch("run_agent.subprocess.run", side_effect=timeout):
            with self.assertRaises(run_agent.ChildFailure) as raised:
                run_agent.run_task("router_scout", "搜索仓库")
        self.assertEqual(
            raised.exception.usage,
            {"input_tokens": 37, "cached_input_tokens": 11, "output_tokens": 5},
        )

    def test_explicit_objective_id_must_match_receipt(self):
        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(run_agent.ChildFailure, "different objective_id"):
                run_agent.run_task("router_scout", "搜索仓库", objective_id="1" * 64)

    def test_glm_objective_mismatch_falls_back_before_success_recording(self):
        expected = "1" * 64

        def fake_invoke(role, task, executor, workspace, timeout, images, env, home, objective_id):
            receipt = valid_receipt()
            if executor.provider != provider_policy.GLM_PROVIDER_ID:
                receipt["objective_id"] = objective_id
            return json.dumps(receipt), None, {
                "input_tokens": 3,
                "cached_input_tokens": 0,
                "output_tokens": 1,
            }, 5

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "run_agent._invoke", side_effect=fake_invoke
        ), mock.patch("run_agent.record_glm_success") as success, mock.patch(
            "run_agent.record_glm_failure"
        ) as failure:
            receipt = run_agent.run_task(
                "router_reviewer",
                "审查当前仓库多个模块",
                execution_profile="GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                codex_home=tmp,
                objective_id=expected,
            )
        success.assert_not_called()
        failure.assert_called_once()
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_reviewer")
        self.assertEqual(receipt["_router_meta"]["usage"]["input_tokens"], 6)

    def test_local_objective_mismatch_falls_back_before_success_recording(self):
        expected = "2" * 64

        def fake_invoke(role, task, executor, workspace, timeout, images, env, home, objective_id):
            receipt = valid_receipt()
            if executor.provider == "openai":
                receipt["objective_id"] = objective_id
            return json.dumps(receipt), None, {
                "input_tokens": 2,
                "cached_input_tokens": 0,
                "output_tokens": 1,
            }, 4

        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent._invoke", side_effect=fake_invoke), mock.patch(
                "run_agent.record_local_success"
            ) as success, mock.patch("run_agent.record_local_failure") as failure:
                receipt = run_agent.run_task(
                    "router_scout",
                    "搜索当前仓库所有文件",
                    light_profile="LOCAL_TEXT_FIRST",
                    env={"LOCAL_MODEL_API_KEY": "test-local-key"},
                    codex_home=tmp,
                    objective_id=expected,
                )
        success.assert_not_called()
        failure.assert_called_once()
        self.assertEqual(receipt["_router_meta"]["executor"], "luna_scout")
        self.assertEqual(receipt["_router_meta"]["usage"]["input_tokens"], 4)

    def test_mcp_tool_schema_excludes_model_monitor(self):
        tool = router_mcp.tool_definition()
        self.assertEqual(tool["name"], "route_task")
        roles = tool["inputSchema"]["properties"]["role"]["enum"]
        self.assertEqual(set(roles), set(run_agent.ROLE_SETTINGS) - {"router_monitor"})
        self.assertEqual(router_mcp.wait_tool_definition()["name"], "wait_for_condition")
        profiles = tool["inputSchema"]["properties"]["execution_profile"]["enum"]
        self.assertEqual(set(profiles), provider_policy.EXECUTION_PROFILES)
        light_profiles = tool["inputSchema"]["properties"]["light_profile"]["enum"]
        self.assertEqual(set(light_profiles), provider_policy.LIGHT_PROFILES)

    def test_deterministic_wait_completes_without_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ready.log"
            marker.write_text("boot\nREADY\n", encoding="utf-8")
            result = router_mcp.wait_for_condition(
                {
                    "condition": "file_contains",
                    "target": str(marker),
                    "expected": "READY",
                    "timeout_seconds": 2,
                    "interval_seconds": 0.2,
                }
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["_router_meta"]["provider"], "deterministic")
        self.assertEqual(result["_router_meta"]["usage"]["input_tokens"], 0)
        self.assertGreaterEqual(result["_router_meta"]["duration_ms"], 0)

    def test_deterministic_wait_honors_cancellation(self):
        cancel = threading.Event()
        cancel.set()
        result = router_mcp.wait_for_condition(
            {
                "condition": "process_exit",
                "target": str(os.getpid()),
                "timeout_seconds": 60,
                "interval_seconds": 30,
            },
            cancel,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertLess(result["elapsed_seconds"], 1)

    def test_mcp_main_processes_cancel_notification_during_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            data = base / "data"
            home.mkdir()
            (home / "config.toml").write_text(
                '[plugins."codex-smart-router@personal"]\nenabled = true\n', encoding="utf-8"
            )
            args = {
                "decision_id": "3" * 64,
                "lease_id": "4" * 32,
                "condition": "file_exists",
                "target": str(base / "never-created"),
                "timeout_seconds": 60,
                "interval_seconds": 30,
            }
            state = router_core.default_state("cancel-session")
            state["mode"] = "ON"
            state["last_decision"] = {
                "decision": "DELEGATE",
                "decision_id": args["decision_id"],
                "lease_id": args["lease_id"],
                "role": "router_monitor",
            }
            state["current_delegation"] = {
                "decision_id": args["decision_id"],
                "lease_id": args["lease_id"],
                "role": "router_monitor",
                "task_digest": router_core.delegation_task_digest("wait_for_condition", args),
                "status": "started",
            }
            router_core.save_state(data, "cancel-session", state)
            call = {"jsonrpc": "2.0", "id": 91, "method": "tools/call", "params": {"name": "wait_for_condition", "arguments": args}}
            cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 91, "reason": "test"}}
            environment = os.environ.copy()
            environment.update({"CODEX_HOME": str(home), "PLUGIN_DATA": str(data)})
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "router_mcp.py")],
                input=json.dumps(call) + "\n" + json.dumps(cancel) + "\n",
                text=True,
                capture_output=True,
                timeout=5,
                env=environment,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.strip())
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], "cancelled")

    def test_deterministic_wait_has_one_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "router_mcp.time.monotonic", side_effect=[0.0, 0.0, 1.1, 1.1]
        ), mock.patch("router_mcp.time.sleep") as sleep:
            result = router_mcp.wait_for_condition(
                {
                    "condition": "file_exists",
                    "target": str(Path(tmp) / "never-created"),
                    "timeout_seconds": 1,
                    "interval_seconds": 0.2,
                }
            )
        self.assertEqual(result["status"], "timeout")
        sleep.assert_called_once()

    def test_glm_first_uses_glm_max_outside_peak(self):
        calls = []

        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            calls.append(command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task(
                "router_reviewer",
                "请独立做一次代码审查",
                execution_profile="GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                codex_home=tmp,
            )
        self.assertEqual(receipt["_router_meta"]["model"], "glm-5.3")
        self.assertEqual(receipt["_router_meta"]["reasoning_effort"], "max")
        self.assertIn('model_provider="zhipu_glm_coding"', calls[0])
        self.assertNotIn("test-key", " ".join(calls[0]))

    def test_child_shell_filter_excludes_glm_key(self):
        command = run_agent.build_command(
            "router_reviewer",
            Path("receipt.json"),
            provider_policy.EXECUTORS["glm_reviewer"],
        )
        self.assertIn('shell_environment_policy.exclude=["^ZHIPU_API_KEY$"]', command)
        self.assertNotIn("any-real-secret", " ".join(command))

    def test_partial_environment_override_preserves_runtime_path(self):
        environment, key = run_agent._child_env(
            provider_policy.EXECUTORS["glm_reviewer"],
            {provider_policy.GLM_ENV_KEY: "test-key"},
        )
        self.assertEqual(key, "test-key")
        self.assertEqual(environment[provider_policy.GLM_ENV_KEY], "test-key")
        self.assertEqual(environment.get("PATH"), os.environ.get("PATH"))

    def test_local_text_first_uses_custom_responses_provider_without_key_in_command(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs["env"]))
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                receipt = run_agent.run_task(
                    "router_scout",
                    "搜索仓库中的测试文件",
                    light_profile="LOCAL_TEXT_FIRST",
                    env={"LOCAL_MODEL_API_KEY": "test-local-key"},
                    codex_home=tmp,
                )
        command, child_env = calls[0]
        self.assertEqual(receipt["_router_meta"]["executor"], "local_scout")
        self.assertEqual(receipt["_router_meta"]["model"], "deepseek-v4-flash")
        self.assertEqual(receipt["_router_meta"]["requested_light_profile"], "LOCAL_TEXT_FIRST")
        self.assertIn('model_provider="local_text_test"', command)
        self.assertIn('model_providers.local_text_test.wire_api="responses"', command)
        self.assertIn('model_providers.local_text_test.env_key="LOCAL_MODEL_API_KEY"', command)
        self.assertIn("^LOCAL_MODEL_API_KEY$", " ".join(command))
        self.assertNotIn("test-local-key", " ".join(command))
        self.assertEqual(child_env["LOCAL_MODEL_API_KEY"], "test-local-key")

    def test_local_runtime_failure_falls_back_to_luna_and_opens_only_local_circuit(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if "deepseek-v4-flash" in command:
                return subprocess.CompletedProcess(command, 1, "", "connection refused")
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                receipt = run_agent.run_task(
                    "router_scout",
                    "搜索仓库中的测试文件",
                    light_profile="LOCAL_TEXT_FIRST",
                    env={"LOCAL_MODEL_API_KEY": "test-local-key"},
                    codex_home=tmp,
                )
            local_health = local_provider.read_health(tmp)
            glm_health = provider_policy.read_health(tmp)
        self.assertEqual(receipt["_router_meta"]["executor"], "luna_scout")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "local_runtime_failure")
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["local_scout", "luna_scout"])
        self.assertEqual(local_health["state"], "open")
        self.assertEqual(glm_health["state"], "closed")
        self.assertEqual(len(calls), 2)

    def test_local_invalid_receipt_falls_back_to_luna(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            output = Path(command[command.index("--output-last-message") + 1])
            receipt = valid_receipt(kwargs.get("input"))
            if "deepseek-v4-flash" in command:
                receipt["evidence"] = [f"item-{index}" for index in range(7)]
            output.write_text(json.dumps(receipt), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                receipt = run_agent.run_task(
                    "router_scout",
                    "搜索仓库中的测试文件",
                    light_profile="LOCAL_TEXT_FIRST",
                    env={"LOCAL_MODEL_API_KEY": "test-local-key"},
                    codex_home=tmp,
                )
        self.assertEqual(receipt["_router_meta"]["executor"], "luna_scout")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "local_runtime_failure")
        self.assertEqual(len(calls), 2)

    def test_local_no_auth_provider_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.local_config(tmp, env_key=None)
            resolution = provider_policy.resolve_executor(
                "router_monitor", light_profile="LOCAL_TEXT_FIRST", env={}, home=tmp
            )
            environment, key = run_agent._child_env(resolution.executor, {}, tmp)
            command = run_agent.build_command(
                "router_monitor", Path(tmp) / "receipt.json", resolution.executor, home=tmp
            )
        self.assertIsNone(key)
        self.assertNotIn("model_providers.local_text_test.env_key", " ".join(command))

    def test_local_config_change_after_resolution_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            resolution = provider_policy.resolve_executor(
                "router_scout",
                light_profile="LOCAL_TEXT_FIRST",
                env={"LOCAL_MODEL_API_KEY": "old-key"},
                home=tmp,
            )
            changed = local_provider.LocalProviderConfig(
                provider_id="local_text_test",
                display_name="Changed Local Text",
                base_url="https://example.invalid/v1",
                model="deepseek-v4-flash",
                env_key="LOCAL_MODEL_API_KEY",
            )
            local_provider.write_config(changed, tmp)
            with self.assertRaisesRegex(run_agent.ChildFailure, "configuration changed"):
                run_agent._child_env(resolution.executor, {"LOCAL_MODEL_API_KEY": "new-key"}, tmp)
            with self.assertRaisesRegex(run_agent.ChildFailure, "configuration changed"):
                run_agent.build_command(
                    "router_scout",
                    Path(tmp) / "receipt.json",
                    resolution.executor,
                    home=tmp,
                )

    def test_glm_quota_error_falls_back_to_terra_and_opens_circuit(self):
        child_calls = []

        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            child_calls.append(command)
            if "glm-5.3" in command:
                return subprocess.CompletedProcess(command, 1, "", '{"code":1317,"next_flush_time":1800000000}')
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task(
                "router_reviewer",
                "请独立做一次代码审查",
                execution_profile="GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                codex_home=tmp,
            )
            health = provider_policy.read_health(tmp)
        self.assertEqual(receipt["_router_meta"]["model"], "gpt-5.6-terra")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "glm_runtime_failure")
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["glm_reviewer", "terra_reviewer"])
        self.assertEqual(health["reason"], "quota_7d")

    def test_glm_writer_failure_after_tool_activity_suppresses_fallback(self):
        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            stdout = json.dumps({"type": "item.completed", "item": {"type": "command_execution"}})
            return subprocess.CompletedProcess(command, 1, stdout, '{"code":1305}')

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(run_agent.ChildFailure, "automatic writer fallback was suppressed"):
                run_agent.run_task(
                    "router_worker",
                    "实现这个边界清晰的小功能",
                    execution_profile="GLM_FIRST",
                    workspace=tmp,
                    now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    env={provider_policy.GLM_ENV_KEY: "test-key"},
                    codex_home=tmp,
                )

    def test_writer_failure_without_complete_evidence_suppresses_fallback(self):
        self.assertTrue(run_agent._writer_failure_may_have_mutated("router_worker", None, None, ""))
        self.assertTrue(run_agent._writer_failure_may_have_mutated("router_worker", "clean", "clean", ""))
        start_only = json.dumps({"type": "turn.started"})
        self.assertTrue(
            run_agent._writer_failure_may_have_mutated("router_worker", "clean", "clean", start_only)
        )
        safe_failure_stream = json.dumps({"type": "turn.failed", "error": {"message": "quota"}})
        self.assertFalse(
            run_agent._writer_failure_may_have_mutated(
                "router_worker",
                "clean",
                "clean",
                safe_failure_stream,
            )
        )

    def test_glm_writer_failure_in_non_git_workspace_never_falls_back(self):
        calls = []

        def fake_run(command, **kwargs):
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            calls.append(command)
            stdout = json.dumps({"type": "turn.failed", "error": {"message": "quota"}})
            return subprocess.CompletedProcess(command, 1, stdout, '{"code":1317}')

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(run_agent.ChildFailure, "automatic writer fallback was suppressed"):
                run_agent.run_task(
                    "router_worker",
                    "实现这个边界清晰的小功能",
                    execution_profile="GLM_FIRST",
                    workspace=tmp,
                    now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    env={provider_policy.GLM_ENV_KEY: "test-key"},
                    codex_home=tmp,
                )
        self.assertEqual(len(calls), 1)

    def test_glm_writer_quota_failure_falls_back_only_with_complete_no_write_evidence(self):
        child_calls = []

        def fake_run(command, **kwargs):
            if "rev-parse" in command:
                return subprocess.CompletedProcess(command, 0, str(Path(kwargs["cwd"]).resolve()) + "\n", "")
            if command[:2] == ["git", "status"] or command[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(command, 0, b"", b"")
            child_calls.append(command)
            if "glm-5.3" in command:
                stdout = json.dumps({"type": "turn.failed", "error": {"message": "quota"}})
                return subprocess.CompletedProcess(command, 1, stdout, '{"code":1317}')
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            receipt = run_agent.run_task(
                "router_worker",
                "实现这个边界清晰的小功能",
                execution_profile="GLM_FIRST",
                workspace=tmp,
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                codex_home=tmp,
            )
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["glm_worker", "terra_worker"])

    def test_mcp_version_comes_from_manifest(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(router_mcp.plugin_version(), manifest["version"])

    def test_mcp_marks_blocked_receipt_as_error(self):
        blocked = {**valid_receipt(), "status": "blocked"}
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "route_task",
                "arguments": {
                    "decision_id": "0" * 64,
                    "lease_id": "0" * 32,
                    "role": "router_scout",
                    "task": "搜索文件",
                },
            },
        }
        stream = io.StringIO()
        with mock.patch("router_mcp.routing_enabled", return_value=True), mock.patch(
            "router_mcp.consume_runtime_lease", return_value=True
        ), mock.patch(
            "router_mcp.run_task", return_value=blocked
        ), redirect_stdout(stream):
            router_mcp.handle(request)
        response = json.loads(stream.getvalue())
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], "blocked")

    def test_mcp_wait_timeout_is_an_error_result(self):
        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "wait_for_condition",
                "arguments": {
                    "decision_id": "0" * 64,
                    "lease_id": "0" * 32,
                    "condition": "file_exists",
                    "target": "/tmp/never",
                },
            },
        }
        result = {
            "status": "timeout",
            "condition": "file_exists",
            "observed": "file does not exist",
            "elapsed_seconds": 1.0,
            "_router_meta": {"role": "router_monitor", "duration_ms": 1000, "usage": {}},
        }
        stream = io.StringIO()
        with mock.patch("router_mcp.routing_enabled", return_value=True), mock.patch(
            "router_mcp.consume_runtime_lease", return_value=True
        ), mock.patch("router_mcp.wait_for_condition", return_value=result), redirect_stdout(stream):
            router_mcp.handle(request)
        response = json.loads(stream.getvalue())
        self.assertTrue(response["result"]["isError"])

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
