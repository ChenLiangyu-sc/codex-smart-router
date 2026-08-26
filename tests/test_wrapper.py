from __future__ import annotations

import json
import io
import subprocess
import os
import re
import sys
import tempfile
import threading
import time
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

    def test_semantic_multimodal_requires_images_and_forces_terra(self):
        task = "分析截图内容并列出视觉缺陷"
        with mock.patch("run_agent.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "require at least one local image"):
                run_agent.run_task(
                    "router_reviewer",
                    task,
                    execution_profile="GLM_FIRST",
                    now=dt.datetime(2026, 8, 27, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                )
            run.assert_not_called()

        def fake_invoke(role, prompt, executor, workspace, timeout, images, env, home, objective_id):
            self.assertEqual(executor.id, "terra_reviewer")
            self.assertEqual(len(images), 1)
            receipt = valid_receipt()
            receipt["objective_id"] = objective_id
            return json.dumps(receipt), None, {
                "input_tokens": 1,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
                "_stream_kind": "exec_per_turn",
                "_counter_semantics": "per_turn_sum",
                "_adapter_version": run_agent.USAGE_ADAPTER_VERSION,
            }, 1

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "screen.png"
            image.write_bytes(b"not-decoded-by-routing-test")
            with mock.patch("run_agent._invoke", side_effect=fake_invoke):
                receipt = run_agent.run_task(
                    "router_reviewer",
                    task,
                    execution_profile="GLM_FIRST",
                    images=[str(image)],
                    now=dt.datetime(2026, 8, 27, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                )
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_reviewer")

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
        self.assertEqual(receipt["_router_meta"]["model"], "gpt-5.6-terra")
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_scout")
        self.assertEqual(receipt["_router_meta"]["provider"], "openai")
        self.assertEqual(receipt["_router_meta"]["selected_executor"], "terra_scout")
        self.assertEqual(receipt["_router_meta"]["final_executor"], "terra_scout")
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["terra_scout"])
        self.assertEqual(receipt["_router_meta"]["route_path"], ["terra_scout"])
        self.assertEqual(receipt["_router_meta"]["route_path_label"], "Terra")
        self.assertFalse(receipt["_router_meta"]["fallback_occurred"])
        self.assertIsNone(receipt["_router_meta"]["fallback_stage"])
        self.assertIsNone(receipt["_router_meta"]["fallback_reason_code"])
        self.assertIsNone(receipt["_router_meta"]["selection_bypass_reason"])
        self.assertEqual(receipt["_router_meta"]["requested_luna_mode"], "LUNA_DISABLED")

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
            {
                "input_tokens": 120,
                "cached_input_tokens": 80,
                "cache_write_input_tokens": 0,
                "output_tokens": 25,
                "reasoning_output_tokens": 0,
            },
        )
        self.assertEqual(receipt["_router_meta"]["usage_stream_kind"], "exec_per_turn")
        self.assertEqual(receipt["_router_meta"]["usage_counter_semantics"], "per_turn_sum")
        self.assertEqual(receipt["_router_meta"]["usage_adapter_version"], "codex-jsonl-v2")
        self.assertEqual(receipt["_router_meta"]["attempt_usage"][0]["outcome"], "completed")
        self.assertGreaterEqual(receipt["_router_meta"]["duration_ms"], 0)

    def test_usage_adapter_ignores_repeated_non_turn_snapshots(self):
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "rate_limits.updated",
                        "usage": {"input_tokens": 999, "cached_input_tokens": 900, "output_tokens": 99},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 80,
                            "cache_write_input_tokens": 20,
                            "output_tokens": 25,
                            "reasoning_output_tokens": 7,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "account.updated",
                        "usage": {"input_tokens": 999, "cached_input_tokens": 900, "output_tokens": 99},
                    }
                ),
            ]
        )
        usage = run_agent._stream_usage(stdout)
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["cache_write_input_tokens"], 20)
        self.assertEqual(usage["reasoning_output_tokens"], 7)
        self.assertEqual(usage["_stream_kind"], "exec_per_turn")

    def test_usage_adapter_uses_last_legacy_snapshot_without_summing(self):
        stdout = "\n".join(
            [
                json.dumps({"type": "legacy", "usage": {"input_tokens": 10, "output_tokens": 1}}),
                json.dumps({"type": "legacy", "usage": {"input_tokens": 20, "output_tokens": 2}}),
            ]
        )
        usage = run_agent._stream_usage(stdout)
        self.assertEqual(usage["input_tokens"], 20)
        self.assertEqual(usage["output_tokens"], 2)
        self.assertEqual(usage["_counter_semantics"], "last_observed_snapshot")

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
            {
                "input_tokens": 37,
                "cached_input_tokens": 11,
                "cache_write_input_tokens": 0,
                "output_tokens": 5,
                "reasoning_output_tokens": 0,
                "_stream_kind": "exec_per_turn",
                "_counter_semantics": "per_turn_sum",
                "_adapter_version": "codex-jsonl-v2",
            },
        )

    def test_explicit_objective_id_must_match_receipt(self):
        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(run_agent.ChildFailure, "different objective_id"):
                run_agent.run_task("router_scout", "搜索仓库", objective_id="1" * 64)

    def test_glm_objective_id_is_runtime_bound_without_fallback(self):
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
        success.assert_called_once()
        failure.assert_not_called()
        self.assertEqual(receipt["objective_id"], expected)
        self.assertEqual(receipt["_router_meta"]["executor"], "glm_reviewer")
        self.assertEqual(receipt["_router_meta"]["usage"]["input_tokens"], 3)
        self.assertIn("objective_id_runtime_override", receipt["_router_meta"]["receipt_normalizations"])

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
        success.assert_called_once()
        failure.assert_not_called()
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_scout")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "local_receipt_format_failure")
        self.assertEqual(receipt["_router_meta"]["fallback_reason_code"], "local_receipt_format_failure")
        self.assertTrue(receipt["_router_meta"]["fallback_occurred"])
        self.assertEqual(receipt["_router_meta"]["fallback_stage"], "receipt")
        self.assertEqual(receipt["_router_meta"]["usage"]["input_tokens"], 4)

    def test_glm_wire_adapter_handles_observed_maas_variants(self):
        raw = json.dumps(
            {
                "status": "pass",
                "summary": "arithmetic checked",
                "findings": [{"severity": "info", "message": "2 + 2 = 4"}],
                "evidence": [],
                "evidence_manifest": [
                    {"claim": "direct evaluation", "path": None, "locator": "prompt", "sha256": None}
                ],
                "validation": [{"kind": "arithmetic", "detail": "evaluated directly"}],
                "coverage": {"scope": "full", "checked": 1, "total": 1},
                "parent_verification": [{"check": "Re-evaluate independently"}],
                "changed_files": [],
                "remaining_risks": [],
                "needs_escalation": False,
                "recommended_next_action": "accept",
            }
        )
        adapted, normalizations = run_agent.adapt_json_object_receipt(raw, "a" * 64)
        valid, errors, receipt = run_agent.validate_receipt(adapted)
        self.assertTrue(valid, errors)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["coverage"]["mode"], "full")
        self.assertEqual(receipt["evidence_manifest"][0]["path"], "")
        self.assertIn("findings_objects", normalizations)
        self.assertIn("validation_objects", normalizations)
        self.assertIn("coverage_scope_alias", normalizations)
        self.assertIn("evidence_manifest_null_path", normalizations)

    def test_glm_wire_adapter_rejects_nested_semantic_objects(self):
        raw = json.dumps({"status": "pass", "findings": [{"message": {"nested": True}}]})
        with self.assertRaises(run_agent.ReceiptFormatFailure):
            run_agent.adapt_json_object_receipt(raw, "a" * 64)

    def test_glm_wire_adapter_rejects_empty_or_status_only_receipts(self):
        for value in ({}, {"status": "pass"}):
            with self.subTest(value=value), self.assertRaises(run_agent.ReceiptFormatFailure):
                run_agent.adapt_json_object_receipt(json.dumps(value), "a" * 64)

    def test_glm_wire_adapter_conservatively_normalizes_coverage_shapes(self):
        base = {
            "status": "pass",
            "findings": ["checked"],
            "coverage": {
                "scope": "targeted_files",
                "files_checked": ["a.py", "b.py"],
                "files_total": "3",
            },
        }
        adapted, normalizations = run_agent.adapt_json_object_receipt(json.dumps(base), "a" * 64)
        _, _, receipt = run_agent.validate_receipt(adapted)
        self.assertEqual(receipt["coverage"], {"mode": "targeted", "checked": 2, "total": 3})
        self.assertIn("coverage_mode_conservative", normalizations)
        self.assertIn("coverage_checked_list_count", normalizations)
        self.assertIn("coverage_total_numeric_string", normalizations)

    def test_glm_wire_adapter_never_infers_full_or_unbounded_coverage(self):
        base = {
            "status": "pass",
            "findings": ["checked"],
            "coverage": {
                "mode": "sampled_not_full",
                "checked": "9" * 5000,
                "total": [{"nested": "not countable"}],
            },
        }
        adapted, normalizations = run_agent.adapt_json_object_receipt(json.dumps(base), "a" * 64)
        _, _, receipt = run_agent.validate_receipt(adapted)
        self.assertEqual(receipt["coverage"], {"mode": "targeted", "checked": 0, "total": None})
        self.assertIn("coverage_mode_conservative", normalizations)
        self.assertIn("coverage_checked_conservative", normalizations)
        self.assertIn("coverage_total_conservative", normalizations)
        huge_total = {**base, "coverage": {"mode": "not_full", "checked": 1, "total": 10_000_000}}
        adapted, _ = run_agent.adapt_json_object_receipt(json.dumps(huge_total), "a" * 64)
        _, _, receipt = run_agent.validate_receipt(adapted)
        self.assertEqual(receipt["coverage"], {"mode": "targeted", "checked": 1, "total": None})

    def test_glm_format_failure_does_not_open_health_circuit(self):
        calls = []

        def fake_invoke(role, task, executor, workspace, timeout, images, env, home, objective_id):
            calls.append(executor.id)
            if executor.provider == provider_policy.GLM_PROVIDER_ID:
                return "not-json", None, {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 1}, 5
            receipt = valid_receipt()
            receipt["objective_id"] = objective_id
            return json.dumps(receipt), None, {"input_tokens": 2, "cached_input_tokens": 0, "output_tokens": 1}, 4

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "run_agent._invoke", side_effect=fake_invoke
        ), mock.patch("run_agent.record_glm_failure") as failure:
            receipt = run_agent.run_task(
                "router_reviewer",
                "审查当前仓库多个模块",
                execution_profile="GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                codex_home=tmp,
            )
        failure.assert_not_called()
        self.assertEqual(calls, ["glm_reviewer", "terra_reviewer"])
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_reviewer")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "glm_receipt_format_failure")
        self.assertEqual(
            [item["outcome"] for item in receipt["_router_meta"]["attempt_usage"]],
            ["receipt_format_failure", "completed"],
        )
        self.assertEqual(
            receipt["_router_meta"]["usage"],
            {
                "input_tokens": 5,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_output_tokens": 0,
            },
        )

    def test_glm_format_failure_and_fallback_share_one_deadline(self):
        timeouts = []

        def fake_invoke(role, task, executor, workspace, timeout, images, env, home, objective_id):
            timeouts.append(timeout)
            if executor.provider == provider_policy.GLM_PROVIDER_ID:
                return "not-json", None, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}, 1
            receipt = valid_receipt()
            receipt["objective_id"] = objective_id
            return json.dumps(receipt), None, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}, 1

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "run_agent.time.monotonic", side_effect=[100.0, 101.0, 105.0]
        ), mock.patch("run_agent._invoke", side_effect=fake_invoke), mock.patch(
            "run_agent.record_glm_failure"
        ) as failure:
            receipt = run_agent.run_task(
                "router_reviewer",
                "审查当前仓库多个模块",
                timeout=10,
                execution_profile="GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                codex_home=tmp,
            )
        failure.assert_not_called()
        self.assertEqual(timeouts, [9, 5])
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "glm_receipt_format_failure")
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["glm_reviewer", "terra_reviewer"])

    def test_glm_writer_receipt_failure_never_starts_a_terra_writer(self):
        calls = []

        def fake_invoke(role, task, executor, workspace, timeout, images, env, home, objective_id):
            calls.append(executor.id)
            return "not-json", None, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}, 1

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent._invoke", side_effect=fake_invoke):
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
        self.assertEqual(calls, ["glm_worker"])

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
        with tempfile.TemporaryDirectory() as tmp:
            command = run_agent.build_command(
                "router_reviewer",
                Path("receipt.json"),
                provider_policy.EXECUTORS["glm_reviewer"],
                home=tmp,
            )
        exclude_entry = next(
            value for value in command if value.startswith("shell_environment_policy.exclude=")
        )
        excluded = json.loads(exclude_entry.split("=", 1)[1])
        self.assertIn("^ZHIPU_API_KEY$", excluded)
        self.assertNotIn("any-real-secret", " ".join(command))

    def test_child_shell_filter_excludes_glm_and_deepseek_keys_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp, env_key="DEEPSEEK_FLASH_API_KEY")
            command = run_agent.build_command(
                "router_reviewer",
                Path("receipt.json"),
                provider_policy.EXECUTORS["glm_reviewer"],
                home=tmp,
            )
        exclude_entry = next(
            value for value in command if value.startswith("shell_environment_policy.exclude=")
        )
        excluded = json.loads(exclude_entry.split("=", 1)[1])
        self.assertIn("^ZHIPU_API_KEY$", excluded)
        self.assertIn("^DEEPSEEK_FLASH_API_KEY$", excluded)

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

    def test_local_runtime_failure_falls_back_to_terra_by_default_and_luna_when_enabled(self):
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
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_scout")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "local_runtime_failure")
        self.assertEqual(receipt["_router_meta"]["fallback_stage"], "runtime")
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["local_scout", "terra_scout"])
        self.assertEqual(receipt["_router_meta"]["route_path_label"], "Local Text Test → Terra")
        self.assertEqual(local_health["state"], "open")
        self.assertEqual(glm_health["state"], "closed")
        self.assertEqual(len(calls), 2)

        calls.clear()
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                receipt = run_agent.run_task(
                    "router_scout",
                    "搜索仓库中的测试文件",
                    light_profile="LOCAL_TEXT_FIRST",
                    luna_mode="LUNA_BOUNDED",
                    env={"LOCAL_MODEL_API_KEY": "test-local-key"},
                    codex_home=tmp,
                )
        self.assertEqual(receipt["_router_meta"]["executor"], "luna_scout")
        self.assertEqual(receipt["_router_meta"]["attempted_executors"], ["local_scout", "luna_scout"])
        self.assertEqual(len(calls), 2)

    def test_local_invalid_receipt_falls_back_to_terra(self):
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
        self.assertEqual(receipt["_router_meta"]["executor"], "terra_scout")
        self.assertEqual(receipt["_router_meta"]["fallback_reason"], "local_receipt_format_failure")
        self.assertEqual(len(calls), 2)

    def test_local_failure_falls_back_to_glm_when_luna_disabled(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if "deepseek-v4-flash" in command:
                return subprocess.CompletedProcess(command, 1, "", "connection refused")
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        off_peak = dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                receipt = run_agent.run_task(
                    "router_scout",
                    "搜索仓库中的测试文件",
                    light_profile="LOCAL_TEXT_FIRST",
                    execution_profile="GLM_FIRST",
                    now=off_peak,
                    env={"LOCAL_MODEL_API_KEY": "test-local-key", provider_policy.GLM_ENV_KEY: "glm-key"},
                    codex_home=tmp,
                )
            glm_health = provider_policy.read_health(tmp)
        meta = receipt["_router_meta"]
        self.assertEqual(meta["executor"], "glm_scout")
        self.assertEqual(meta["attempted_executors"], ["local_scout", "glm_scout"])
        self.assertEqual(meta["route_path_label"], "Local Text Test → GLM-5.3")
        self.assertEqual(meta["fallback_reason"], "local_runtime_failure")
        self.assertTrue(meta["fallback_occurred"])
        self.assertEqual(glm_health["state"], "closed")
        self.assertEqual(len(calls), 2)

    def test_chain_never_invokes_more_than_two_models(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            model = command[command.index("--model") + 1]
            if model in {"deepseek-v4-flash", "gpt-5.6-luna"}:
                return subprocess.CompletedProcess(command, 1, "", "connection refused")
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        off_peak = dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                with self.assertRaises(run_agent.ChildFailure):
                    run_agent.run_task(
                        "router_scout",
                        "搜索仓库中的测试文件",
                        light_profile="LOCAL_TEXT_FIRST",
                        execution_profile="GLM_FIRST",
                        luna_mode="LUNA_BOUNDED",
                        now=off_peak,
                        env={"LOCAL_MODEL_API_KEY": "test-local-key", provider_policy.GLM_ENV_KEY: "glm-key"},
                        codex_home=tmp,
                    )
        attempted_models = [command[command.index("--model") + 1] for command in calls]
        self.assertEqual(attempted_models, ["deepseek-v4-flash", "gpt-5.6-luna"])
        self.assertNotIn("glm-5.3", attempted_models)
        self.assertNotIn("gpt-5.6-terra", attempted_models)

    def test_glm_peak_selection_bypass_is_recorded_as_not_attempted(self):
        def fake_run(command, **kwargs):
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(json.dumps(valid_receipt(kwargs.get("input"))), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        peak = dt.datetime(2026, 8, 24, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                receipt = run_agent.run_task(
                    "router_reviewer",
                    "跨文件复核这四个模块的合同一致性问题并归因缺陷",
                    execution_profile="GLM_FIRST",
                    now=peak,
                    env={provider_policy.GLM_ENV_KEY: "glm-key"},
                    codex_home=tmp,
                )
        meta = receipt["_router_meta"]
        self.assertEqual(meta["executor"], "terra_reviewer")
        self.assertEqual(meta["selected_executor"], "terra_reviewer")
        self.assertEqual(meta["attempted_executors"], ["terra_reviewer"])
        self.assertEqual(meta["route_path"], ["terra_reviewer"])
        self.assertFalse(meta["fallback_occurred"])
        self.assertEqual(meta["fallback_stage"], "selection")
        self.assertEqual(meta["fallback_reason_code"], "glm_peak_window")
        self.assertEqual(meta["fallback_reason"], "glm_peak_window")
        self.assertEqual(meta["selection_bypass_reason"], "glm_peak_window")
        self.assertEqual(meta["route_path_label"], "Terra")

    def test_mcp_route_task_passes_luna_mode_and_schema_defaults(self):
        schema = router_mcp.tool_definition()["inputSchema"]["properties"]
        self.assertEqual(schema["luna_mode"]["default"], "LUNA_DISABLED")
        self.assertEqual(schema["luna_mode"]["enum"], ["LUNA_BOUNDED", "LUNA_DISABLED"])
        request = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "route_task",
                "arguments": {
                    "decision_id": "0" * 64,
                    "lease_id": "0" * 32,
                    "role": "router_scout",
                    "task": "搜索文件",
                    "luna_mode": "LUNA_BOUNDED",
                },
            },
        }
        stream = io.StringIO()
        with mock.patch("router_mcp.routing_enabled", return_value=True), mock.patch(
            "router_mcp.consume_runtime_lease", return_value=True
        ), mock.patch(
            "router_mcp.run_task", return_value=valid_receipt()
        ) as task, redirect_stdout(stream):
            router_mcp.handle(request)
        response = json.loads(stream.getvalue())
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(task.call_args.kwargs.get("luna_mode"), "LUNA_BOUNDED")

    def test_local_no_auth_provider_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.local_config(tmp, env_key=None)
            resolution = provider_policy.resolve_executor(
                "router_scout", light_profile="LOCAL_TEXT_FIRST", env={}, home=tmp
            )
            environment, key = run_agent._child_env(resolution.executor, {}, tmp)
            command = run_agent.build_command(
                "router_scout", Path(tmp) / "receipt.json", resolution.executor, home=tmp
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
            with self.assertRaisesRegex(run_agent.RoutedTaskFailure, "automatic writer fallback was suppressed") as raised:
                run_agent.run_task(
                    "router_worker",
                    "实现这个边界清晰的小功能",
                    execution_profile="GLM_FIRST",
                    workspace=tmp,
                    now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    env={provider_policy.GLM_ENV_KEY: "test-key"},
                    codex_home=tmp,
                )
        failure = raised.exception
        self.assertTrue(failure.may_have_mutated)
        meta = failure.router_meta
        # The suppressed writer still leaves a complete single-attempt ledger.
        self.assertEqual(meta["attempted_executors"], ["glm_worker"])
        self.assertEqual(meta["final_executor"], None)
        self.assertEqual(meta["route_path"], ["glm_worker"])
        self.assertEqual(meta["fallback_reason_code"], "glm_runtime_failure")
        self.assertEqual(meta["fallback_stage"], "runtime")
        self.assertEqual(len(meta["attempt_usage"]), 1)
        self.assertEqual(meta["attempt_usage"][0]["outcome"], "runtime_failure")

    def test_double_failure_returns_structured_routed_task_failure(self):
        calls = []

        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            calls.append(command)
            model = command[command.index("--model") + 1]
            usage = json.dumps(
                {"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 7}}
            )
            return subprocess.CompletedProcess(command, 1, usage, "connection reset")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            with self.assertRaises(run_agent.RoutedTaskFailure) as raised:
                run_agent.run_task(
                    "router_reviewer",
                    "请独立做一次代码审查",
                    execution_profile="GLM_FIRST",
                    now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    env={provider_policy.GLM_ENV_KEY: "test-key"},
                    codex_home=tmp,
                )
        meta = raised.exception.router_meta
        self.assertEqual(len(calls), 2, "GLM then Terra must both be attempted before failing")
        self.assertEqual(meta["attempted_executors"], ["glm_reviewer", "terra_reviewer"])
        self.assertEqual(meta["route_path"], ["glm_reviewer", "terra_reviewer"])
        self.assertEqual(meta["route_path_label"], "GLM-5.3 → Terra")
        self.assertIsNone(meta["final_executor"])
        self.assertIsNone(meta["executor"])
        self.assertTrue(meta["fallback_occurred"])
        self.assertEqual(meta["fallback_stage"], "runtime")
        self.assertEqual(meta["fallback_reason_code"], "terra_runtime_failure")
        self.assertEqual(len(meta["attempt_usage"]), 2)
        self.assertEqual([item["outcome"] for item in meta["attempt_usage"]], ["runtime_failure", "runtime_failure"])
        self.assertEqual(meta["usage"]["input_tokens"], 22)
        self.assertEqual(meta["usage"]["output_tokens"], 14)
        self.assertGreaterEqual(meta["duration_ms"], 0)

    def test_deadline_exhausted_before_terra_is_never_terras_failure(self):
        calls = []

        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            calls.append(command)
            time.sleep(1.2)  # exceed the shared deadline during the GLM attempt
            return subprocess.CompletedProcess(command, 1, "", "connection reset")

        with tempfile.TemporaryDirectory() as tmp, mock.patch("run_agent.subprocess.run", side_effect=fake_run):
            with self.assertRaises(run_agent.RoutedTaskFailure) as raised:
                run_agent.run_task(
                    "router_reviewer",
                    "请独立做一次代码审查",
                    timeout=1,
                    execution_profile="GLM_FIRST",
                    now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                    env={provider_policy.GLM_ENV_KEY: "test-key"},
                    codex_home=tmp,
                )
        meta = raised.exception.router_meta
        attempted_models = [command[command.index("--model") + 1] for command in calls]
        self.assertEqual(attempted_models, ["glm-5.3"], "Terra must never be invoked after the deadline is gone")
        self.assertEqual(meta["attempted_executors"], ["glm_reviewer"])
        self.assertEqual(meta["route_path"], ["glm_reviewer"])
        self.assertFalse(meta["fallback_occurred"])
        self.assertEqual(meta["fallback_stage"], "deadline")
        self.assertEqual(meta["fallback_reason_code"], "shared_deadline_exhausted_before_fallback")
        self.assertEqual(len(meta["attempt_usage"]), 1)
        self.assertNotIn("terra_runtime_failure", json.dumps(meta))

    def test_deadline_exhausted_on_local_chain_skips_every_remaining_candidate(self):
        calls = []

        def fake_run(command, **kwargs):
            if "--output-last-message" not in command:
                return subprocess.CompletedProcess(command, 1, "", "")
            calls.append(command)
            time.sleep(1.2)  # exceed the shared deadline during the local attempt
            return subprocess.CompletedProcess(command, 1, "", "connection reset")

        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            with mock.patch("run_agent.subprocess.run", side_effect=fake_run):
                with self.assertRaises(run_agent.RoutedTaskFailure) as raised:
                    run_agent.run_task(
                        "router_scout",
                        "搜索仓库中的测试文件",
                        timeout=1,
                        light_profile="LOCAL_TEXT_FIRST",
                        luna_mode="LUNA_BOUNDED",
                        execution_profile="GLM_FIRST",
                        now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
                        env={
                            "LOCAL_MODEL_API_KEY": "test-local-key",
                            provider_policy.GLM_ENV_KEY: "test-key",
                        },
                        codex_home=tmp,
                    )
            local_health = local_provider.read_health(tmp)
        meta = raised.exception.router_meta
        attempted_models = [command[command.index("--model") + 1] for command in calls]
        self.assertEqual(attempted_models, ["deepseek-v4-flash"], "Luna/GLM/Terra must not run after the deadline")
        self.assertEqual(meta["attempted_executors"], ["local_scout"])
        self.assertFalse(meta["fallback_occurred"])
        self.assertEqual(meta["fallback_stage"], "deadline")
        self.assertEqual(meta["fallback_reason_code"], "shared_deadline_exhausted_before_fallback")
        self.assertEqual(local_health["state"], "open", "the real local failure still opens its own circuit")

    def test_run_task_rejects_monitor_role_outside_wait_tool(self):
        with mock.patch("run_agent.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "wait_for_condition"):
                run_agent.run_task("router_monitor", "等待构建完成")
            run.assert_not_called()

    def test_mcp_returns_structured_failure_ledger_for_routed_task_failure(self):
        failure_meta = {
            "role": "router_reviewer",
            "attempted_executors": ["glm_reviewer", "terra_reviewer"],
            "route_path": ["glm_reviewer", "terra_reviewer"],
            "route_path_label": "GLM-5.3 → Terra",
            "fallback_occurred": True,
            "fallback_stage": "runtime",
            "fallback_reason_code": "terra_runtime_failure",
            "attempt_usage": [],
        }
        request = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "route_task",
                "arguments": {
                    "decision_id": "0" * 64,
                    "lease_id": "0" * 32,
                    "role": "router_reviewer",
                    "task": "请独立做一次代码审查",
                },
            },
        }
        stream = io.StringIO()
        with mock.patch("router_mcp.routing_enabled", return_value=True), mock.patch(
            "router_mcp.consume_runtime_lease", return_value=True
        ), mock.patch(
            "router_mcp.run_task",
            side_effect=run_agent.RoutedTaskFailure(
                "Codex child failed with exit 1: terra transport",
                router_meta=failure_meta,
            ),
        ), redirect_stdout(stream):
            router_mcp.handle(request)
        response = json.loads(stream.getvalue())
        self.assertTrue(response["result"]["isError"])
        structured = response["result"]["structuredContent"]
        self.assertEqual(structured["status"], "failed")
        self.assertEqual(structured["_router_meta"]["fallback_reason_code"], "terra_runtime_failure")
        self.assertEqual(structured["_router_meta"]["route_path"], ["glm_reviewer", "terra_reviewer"])

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

    def test_mcp_fails_closed_when_semantic_multimodal_task_omits_images(self):
        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "route_task",
                "arguments": {
                    "decision_id": "0" * 64,
                    "lease_id": "0" * 32,
                    "role": "router_reviewer",
                    "execution_profile": "GLM_FIRST",
                    "task": "分析截图内容并列出视觉缺陷",
                },
            },
        }
        stream = io.StringIO()
        with mock.patch("router_mcp.routing_enabled", return_value=True), mock.patch(
            "router_mcp.consume_runtime_lease", return_value=True
        ), mock.patch("run_agent.subprocess.run") as run, redirect_stdout(stream):
            router_mcp.handle(request)
        response = json.loads(stream.getvalue())
        self.assertTrue(response["result"]["isError"])
        self.assertIn("require at least one local image", response["result"]["content"][0]["text"])
        run.assert_not_called()

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
