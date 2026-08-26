from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "router_hook.py"
sys.path.insert(0, str(ROOT / "scripts"))

import router_core
import local_provider
import install_agents


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.session = "test-session"
        self.default_home = self.ready_home("default-home")
        self.previous_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.default_home)

    def tearDown(self):
        if self.previous_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.previous_codex_home
        self.temp.cleanup()

    def call(self, event, payload, extra_env=None):
        env = os.environ.copy()
        env["PLUGIN_DATA"] = str(self.data)
        env["CODEX_HOME"] = str(self.default_home)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [sys.executable, str(HOOK), event],
            input=json.dumps({"session_id": self.session, **payload}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def prompt(self, text):
        return self.call("user-prompt-submit", {"prompt": text})

    def ready_home(self, name="ready-home"):
        ready_home = self.data / name
        agents = ready_home / "agents"
        agents.mkdir(parents=True)
        for role in router_core.ROLES:
            (agents / f"{role}.toml").write_text("placeholder\n", encoding="utf-8")
        (ready_home / "config.toml").write_text("[mcp_servers.smart_router]\n", encoding="utf-8")
        return ready_home

    def test_off_and_ready_new_session_are_silent(self):
        ready_home = self.data / "ready-home"
        agents = ready_home / "agents"
        agents.mkdir(parents=True)
        for role in router_core.ROLES:
            (agents / f"{role}.toml").write_text("placeholder\n", encoding="utf-8")
        (ready_home / "config.toml").write_text("[mcp_servers.smart_router]\n", encoding="utf-8")
        started = self.call("session-start", {}, {"CODEX_HOME": str(ready_home)})
        self.assertIsNone(started)
        self.assertIsNone(self.prompt("实现一个功能"))

    def test_missing_and_parked_setup_messages_are_actionable(self):
        empty_home = self.data / "empty-home"
        empty_home.mkdir()
        missing = self.call("session-start", {}, {"CODEX_HOME": str(empty_home)})
        missing_text = missing["hookSpecificOutput"]["additionalContext"]
        self.assertIn("--apply", missing_text)
        self.assertIn(str(ROOT / "scripts" / "install_agents.py"), missing_text)

        ready_home = self.data / "parked-home"
        agents = ready_home / "agents"
        agents.mkdir(parents=True)
        for role in router_core.ROLES:
            (agents / f"{role}.toml").write_text("placeholder\n", encoding="utf-8")
        (ready_home / "config.toml").write_text("[mcp_servers.smart_router]\n", encoding="utf-8")
        marker = ready_home / "smart-router" / "DISABLED"
        marker.parent.mkdir()
        marker.write_text("parked\n", encoding="utf-8")
        parked = self.call("session-start", {}, {"CODEX_HOME": str(ready_home)})
        self.assertIn("--enable", parked["hookSpecificOutput"]["additionalContext"])

        parked_on = self.call(
            "user-prompt-submit",
            {"prompt": "$router-control 开启"},
            {"CODEX_HOME": str(ready_home)},
        )
        self.assertIn("尚未开启", parked_on["hookSpecificOutput"]["additionalContext"])
        self.assertIn("--enable", parked_on["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(router_core.load_state(self.data, self.session)["mode"], "OFF")

    def test_on_refuses_missing_runtime_but_shadow_remains_available(self):
        empty_home = self.data / "missing-runtime"
        empty_home.mkdir()
        refused = self.call(
            "user-prompt-submit",
            {"prompt": "$router-control 开启"},
            {"CODEX_HOME": str(empty_home)},
        )
        text = refused["hookSpecificOutput"]["additionalContext"]
        self.assertIn("尚未开启", text)
        self.assertIn("--apply", text)
        self.assertEqual(router_core.load_state(self.data, self.session)["mode"], "OFF")

        shadow = self.call(
            "user-prompt-submit",
            {"prompt": "$router-control 影子模式"},
            {"CODEX_HOME": str(empty_home)},
        )
        self.assertIn("已开启影子模式", shadow["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(router_core.load_state(self.data, self.session)["mode"], "SHADOW")

    def test_control_persists_and_on_routes(self):
        control = self.prompt("$router-control 开启")
        self.assertIn("边界清晰且适合委派", control["hookSpecificOutput"]["additionalContext"])
        routed = self.prompt("搜索仓库并盘点文件")
        context = routed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("SR_ON DELEGATE", context)
        self.assertIn("router_scout", context)
        status = self.prompt("$router-control 状态")
        self.assertIn("智能路由：已开启", status["hookSpecificOutput"]["additionalContext"])
        self.assertIn("最近建议：Luna · 只读侦察", status["hookSpecificOutput"]["additionalContext"])

    def test_glm_profile_activates_persists_and_is_bound_to_wrapper_calls(self):
        ready_home = self.ready_home("glm-ready-home")
        env = {"CODEX_HOME": str(ready_home)}
        control = self.call(
            "user-prompt-submit",
            {"prompt": "$router-control glm 开启"},
            env,
        )
        self.assertIn("GLM_FIRST", control["hookSpecificOutput"]["additionalContext"])
        state = router_core.load_state(self.data, self.session)
        self.assertEqual(state["mode"], "ON")
        self.assertEqual(state["execution_profile"], "GLM_FIRST")

        routed = self.call(
            "user-prompt-submit",
            {"prompt": "请独立做一次代码审查"},
            env,
        )
        context = routed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("role=router_reviewer profile=GLM_FIRST", context)
        native = self.call(
            "pre-tool-use",
            {"tool_input": {"agent_type": "router_reviewer", "fork_turns": "none"}},
            env,
        )
        self.assertIn("smart_router.route_task", native["hookSpecificOutput"]["permissionDecisionReason"])
        wrong_profile = self.call(
            "pre-tool-use",
            {
                "tool_name": "mcp__smart_router__route_task",
                "tool_input": {
                    "role": "router_reviewer",
                    "task": "请独立做一次代码审查",
                    "execution_profile": "STABLE",
                },
            },
            env,
        )
        self.assertIn("profile", wrong_profile["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(
            self.call(
                "pre-tool-use",
                {
                    "tool_name": "mcp__smart_router__route_task",
                    "tool_input": {
                        "role": "router_reviewer",
                        "task": "请独立做一次代码审查",
                        "execution_profile": "GLM_FIRST",
                    },
                },
                env,
            )
        )
        status = self.call("user-prompt-submit", {"prompt": "$router-control 状态"}, env)
        status_text = status["hookSpecificOutput"]["additionalContext"]
        self.assertIn("重任务：GLM_FIRST", status_text)
        self.assertIn("最近建议：GLM-5.3 Max / Terra 动态执行", status_text)

        disabled = self.call(
            "user-prompt-submit",
            {"prompt": "$router-control glm 关闭"},
            env,
        )
        self.assertIn("恢复 STABLE", disabled["hookSpecificOutput"]["additionalContext"])
        state = router_core.load_state(self.data, self.session)
        self.assertEqual(state["mode"], "ON")
        self.assertEqual(state["execution_profile"], "STABLE")

    def test_local_profile_is_session_scoped_and_forces_dynamic_wrapper(self):
        ready_home = self.ready_home("local-ready-home")
        config = local_provider.LocalProviderConfig(
            provider_id="local_text_test",
            display_name="GLM-5.3 surrogate",
            base_url="https://open.bigmodel.cn/api/v1",
            model="glm-5.3",
            env_key="ZHIPU_API_KEY",
            surrogate="DeepSeek V4 Flash routing surrogate",
        )
        local_provider.write_config(config, ready_home)
        secret = ready_home / "smart-router" / "providers.env"
        secret.write_text("ZHIPU_API_KEY=test-key\n", encoding="utf-8")
        os.chmod(secret, 0o600)
        env = {"CODEX_HOME": str(ready_home)}

        control = self.call("user-prompt-submit", {"prompt": "$router-control local 开启"}, env)
        self.assertIn("LOCAL_TEXT_FIRST", control["hookSpecificOutput"]["additionalContext"])
        state = router_core.load_state(self.data, self.session)
        self.assertEqual(state["mode"], "ON")
        self.assertEqual(state["light_profile"], "LOCAL_TEXT_FIRST")

        routed = self.call("user-prompt-submit", {"prompt": "搜索仓库并盘点文件"}, env)
        context = routed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("light=LOCAL_TEXT_FIRST", context)
        native = self.call(
            "pre-tool-use",
            {"tool_input": {"agent_type": "router_scout", "fork_turns": "none"}},
            env,
        )
        self.assertIn("smart_router.route_task", native["hookSpecificOutput"]["permissionDecisionReason"])
        wrong = self.call(
            "pre-tool-use",
            {
                "tool_name": "mcp__smart_router__route_task",
                "tool_input": {
                    "role": "router_scout",
                    "task": "搜索仓库并盘点文件",
                    "light_profile": "LUNA_STABLE",
                },
            },
            env,
        )
        self.assertIn("light profile", wrong["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(
            self.call(
                "pre-tool-use",
                {
                    "tool_name": "mcp__smart_router__route_task",
                    "tool_input": {
                        "role": "router_scout",
                        "task": "搜索仓库并盘点文件",
                        "light_profile": "LOCAL_TEXT_FIRST",
                    },
                },
                env,
            )
        )
        status = self.call("user-prompt-submit", {"prompt": "$router-control 状态"}, env)
        self.assertIn("轻任务：LOCAL_TEXT_FIRST", status["hookSpecificOutput"]["additionalContext"])
        self.assertIn("GLM-5.3 surrogate 可用", status["hookSpecificOutput"]["additionalContext"])

        disabled = self.call("user-prompt-submit", {"prompt": "$router-control local 关闭"}, env)
        self.assertIn("LUNA_STABLE", disabled["hookSpecificOutput"]["additionalContext"])
        state = router_core.load_state(self.data, self.session)
        self.assertEqual(state["mode"], "ON")
        self.assertEqual(state["light_profile"], "LUNA_STABLE")

    def test_glm_profile_keeps_luna_for_light_roles_and_rejects_images(self):
        ready_home = self.ready_home("glm-luna-home")
        env = {"CODEX_HOME": str(ready_home)}
        self.call("user-prompt-submit", {"prompt": "$router-control glm 开启"}, env)
        routed = self.call(
            "user-prompt-submit",
            {"prompt": "搜索仓库并盘点文件"},
            env,
        )
        self.assertIn("role=router_scout profile=GLM_FIRST", routed["hookSpecificOutput"]["additionalContext"])
        denied = self.call(
            "pre-tool-use",
            {
                "tool_name": "mcp__smart_router__route_task",
                "tool_input": {
                    "role": "router_scout",
                    "task": "搜索仓库并盘点文件",
                    "execution_profile": "GLM_FIRST",
                    "images": ["screen.png"],
                },
            },
            env,
        )
        self.assertIn("Terra-capable", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_shadow_never_delegates(self):
        self.prompt("$router-control 影子模式")
        result = self.prompt("搜索仓库并盘点文件")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("SR_SHADOW", context)
        self.assertIn("路由预览：Luna · 只读侦察", context)

    def test_high_risk_stays_sol_when_on(self):
        self.prompt("/router on")
        result = self.prompt("实现生产数据库迁移并部署")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("INLINE_SOL", context)
        self.assertIn("risk=HIGH", context)

    def test_posttool_records_actual_read_only_execution_for_status(self):
        self.prompt("/router on")
        self.prompt("搜索仓库并盘点文件")
        call = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-scout-1",
            "tool_input": {"role": "router_scout", "task": "搜索仓库并盘点文件"},
            "tool_response": {"isError": False},
        }
        self.assertIsNone(self.call("post-tool-use", call))
        status = self.prompt("$router-control 状态")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("最近实际执行：Luna · 只读侦察（成功）", status)
        self.assertIn("累计实际执行：成功 1，失败 0", status)
        # Duplicate runtime events must not inflate the user's execution count.
        self.assertIsNone(self.call("post-tool-use", call))
        status = self.prompt("$router-control 状态")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("累计实际执行：成功 1，失败 0", status)
        second = {**call, "tool_use_id": "mcp-scout-2"}
        self.assertIsNone(self.call("post-tool-use", second))
        # Delayed A after A -> B must also be idempotent, not only adjacent A -> A.
        self.assertIsNone(self.call("post-tool-use", call))
        status = self.prompt("$router-control 状态")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("累计实际执行：成功 2，失败 0", status)

    def test_blocked_receipt_counts_as_failed_actual_execution(self):
        self.prompt("/router on")
        self.prompt("搜索仓库并盘点文件")
        call = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-scout-blocked",
            "tool_input": {"role": "router_scout", "task": "搜索仓库并盘点文件"},
            "tool_response": {
                "isError": False,
                "structuredContent": {"status": "blocked"},
            },
        }
        self.assertIsNone(self.call("post-tool-use", call))
        status = self.prompt("$router-control 状态")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("最近实际执行：Luna · 只读侦察（失败）", status)
        self.assertIn("累计实际执行：成功 0，失败 1", status)

    def test_pretool_guard(self):
        denied = self.call(
            "pre-tool-use",
            {"tool_input": {"agent_type": "router_scout", "fork_turns": "none"}},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.prompt("/router on")
        self.prompt("搜索仓库并盘点文件")
        self.assertIsNone(
            self.call(
                "pre-tool-use",
                {"tool_input": {"agent_type": "router_scout", "fork_turns": "none"}},
            )
        )
        wrong = self.call(
            "pre-tool-use",
            {"tool_input": {"agent_type": "router_worker", "fork_turns": "none"}},
        )
        self.assertEqual(wrong["hookSpecificOutput"]["permissionDecision"], "deny")
        inherited = self.call(
            "pre-tool-use",
            {"tool_input": {"agent_type": "router_scout"}},
        )
        self.assertIn("fork_turns=none", inherited["hookSpecificOutput"]["permissionDecisionReason"])

    def test_wrapper_tool_guard(self):
        self.prompt("/router on")
        self.prompt("搜索仓库并盘点文件")
        allowed = self.call(
            "pre-tool-use",
            {
                "tool_name": "mcp__smart_router__route_task",
                "tool_input": {"role": "router_scout", "task": "搜索仓库并盘点文件"},
            },
        )
        self.assertIsNone(allowed)
        wrong = self.call(
            "pre-tool-use",
            {
                "tool_name": "mcp__smart_router__route_task",
                "tool_input": {"role": "router_worker", "task": "搜索仓库并盘点文件"},
            },
        )
        self.assertEqual(wrong["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_receipt_gets_one_repair_only(self):
        self.prompt("/router on")
        self.prompt("补充单元测试用例")
        self.call(
            "pre-tool-use",
            {
                "tool_use_id": "native-tester-1",
                "tool_input": {"agent_type": "router_tester", "fork_turns": "none"},
            },
        )
        first = self.call(
            "subagent-stop",
            {"agent_type": "router_tester", "last_assistant_message": "not json", "stop_hook_active": False},
        )
        self.assertEqual(first["decision"], "block")
        second = self.call(
            "subagent-stop",
            {"agent_type": "router_tester", "last_assistant_message": "still not json", "stop_hook_active": True},
        )
        self.assertIsNone(second)

    def test_valid_receipt_releases_writer(self):
        self.prompt("/router on")
        self.prompt("实现这个边界清晰的修复")
        args = {
            "tool_use_id": "native-worker-1",
            "tool_input": {"agent_type": "router_worker", "fork_turns": "none"},
        }
        self.assertIsNone(self.call("pre-tool-use", args))
        busy = self.call("pre-tool-use", args)
        self.assertIn("already active", busy["hookSpecificOutput"]["permissionDecisionReason"])
        receipt = {
            "status": "completed",
            "summary": "done",
            "findings": [],
            "evidence": [],
            "changed_files": ["a.py"],
            "validation": ["tests passed"],
            "remaining_risks": [],
            "needs_escalation": False,
            "recommended_next_action": "integrate",
        }
        self.assertIsNone(
            self.call(
                "subagent-stop",
                {"agent_type": "router_worker", "last_assistant_message": json.dumps(receipt)},
            )
        )
        self.assertIsNone(self.call("pre-tool-use", args))

    def test_mcp_writer_lease_releases_after_success_and_failure(self):
        self.prompt("/router on")
        self.prompt("实现这个边界清晰的小功能")
        first = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-worker-1",
            "tool_input": {"role": "router_worker", "task": "新建 first.txt 并写入 first"},
        }
        second = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-worker-2",
            "tool_input": {"role": "router_worker", "task": "新建 second.txt 并写入 second"},
        }
        self.assertIsNone(self.call("pre-tool-use", first))
        busy = self.call("pre-tool-use", second)
        self.assertIn("already active", busy["hookSpecificOutput"]["permissionDecisionReason"])

        self.assertIsNone(self.call("post-tool-use", {**first, "tool_response": {"isError": False}}))
        self.assertIsNone(self.call("pre-tool-use", second))
        self.assertIsNone(self.call("post-tool-use", {**second, "tool_response": {"isError": True}}))

        third = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-worker-3",
            "tool_input": {"role": "router_worker", "task": "新建 third.txt 并写入 third"},
        }
        self.assertIsNone(self.call("pre-tool-use", third))

    def test_mode_change_preserves_inflight_writer_until_posttool(self):
        workspace = self.data / "mode-workspace"
        workspace.mkdir()
        self.prompt("/router on")
        self.prompt("实现这个边界清晰的小功能")
        call = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-mode-change",
            "cwd": str(workspace),
            "tool_input": {"role": "router_worker", "task": "新建 mode.txt 并写入 mode"},
        }
        self.assertIsNone(self.call("pre-tool-use", call))
        lock_path = router_core.writer_lock_path(workspace)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.prompt("/router off")
            state = router_core.load_state(self.data, self.session)
            self.assertIsNotNone(state["active_writer"])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertIsNone(
            self.call(
                "post-tool-use",
                {
                    **call,
                    "tool_response": {
                        "isError": False,
                        "structuredContent": {"status": "completed"},
                    },
                },
            )
        )
        state = router_core.load_state(self.data, self.session)
        self.assertIsNone(state["active_writer"])
        self.assertEqual(state["execution_counts"], {"completed": 1, "failed": 0})

    def test_next_user_turn_recovers_missed_mcp_posttool_release(self):
        self.prompt("/router on")
        self.prompt("新建 first.txt 并写入 first")
        first = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-missed-posttool",
            "turn_id": "turn-1",
            "tool_input": {"role": "router_worker", "task": "新建 first.txt 并写入 first"},
        }
        self.assertIsNone(self.call("pre-tool-use", first))

        # Simulate a runtime path that never delivered PostToolUse. A new user
        # task is a safe recovery boundary and must not inherit the stale lease.
        self.prompt("新建 second.txt 并写入 second")
        second = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-next-turn",
            "turn_id": "turn-2",
            "tool_input": {"role": "router_worker", "task": "新建 second.txt 并写入 second"},
        }
        self.assertIsNone(self.call("pre-tool-use", second))

    def test_next_user_turn_never_recovers_a_live_workspace_writer(self):
        workspace = self.data / "workspace"
        workspace.mkdir()
        self.prompt("/router on")
        self.prompt("新建 first.txt 并写入 first")
        first = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-live-writer",
            "turn_id": "turn-1",
            "cwd": str(workspace),
            "tool_input": {"role": "router_worker", "task": "新建 first.txt 并写入 first"},
        }
        self.assertIsNone(self.call("pre-tool-use", first))

        lock_path = router_core.writer_lock_path(workspace)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.prompt("新建 second.txt 并写入 second")
            blocked = self.call(
                "pre-tool-use",
                {
                    **first,
                    "tool_use_id": "mcp-concurrent-writer",
                    "turn_id": "turn-2",
                    "tool_input": {"role": "router_worker", "task": "新建 second.txt 并写入 second"},
                },
            )
            self.assertIn("already active", blocked["hookSpecificOutput"]["permissionDecisionReason"])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_mcp_posttool_cannot_release_a_different_call_lease(self):
        self.prompt("/router on")
        self.prompt("实现这个边界清晰的小功能")
        active = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-active",
            "tool_input": {"role": "router_worker", "task": "新建 active.txt 并写入 active"},
        }
        self.assertIsNone(self.call("pre-tool-use", active))
        mismatched = {**active, "tool_use_id": "mcp-other", "tool_response": {"isError": False}}
        self.assertIsNone(self.call("post-tool-use", mismatched))
        blocked = self.call(
            "pre-tool-use",
            {**active, "tool_use_id": "mcp-next"},
        )
        self.assertIn("already active", blocked["hookSpecificOutput"]["permissionDecisionReason"])

    def test_mcp_posttool_cannot_release_a_different_role_lease(self):
        self.prompt("/router on")
        self.prompt("实现这个边界清晰的小功能")
        active = {
            "tool_name": "mcp__smart_router__route_task",
            "tool_use_id": "mcp-role-bound",
            "tool_input": {"role": "router_worker", "task": "新建 active.txt 并写入 active"},
        }
        self.assertIsNone(self.call("pre-tool-use", active))
        mismatched = {
            **active,
            "tool_input": {"role": "router_docs", "task": "更新 README 文档"},
            "tool_response": {"isError": False},
        }
        self.assertIsNone(self.call("post-tool-use", mismatched))
        blocked = self.call("pre-tool-use", {**active, "tool_use_id": "mcp-next"})
        self.assertIn("already active", blocked["hookSpecificOutput"]["permissionDecisionReason"])
        # A malformed mismatched event must not prevent the later correct event
        # from releasing the actual writer or count as an execution.
        self.assertIsNone(self.call("post-tool-use", {**active, "tool_response": {"isError": False}}))
        state = router_core.load_state(self.data, self.session)
        self.assertIsNone(state["active_writer"])
        self.assertEqual(state["execution_counts"], {"completed": 1, "failed": 0})

    def test_hook_configuration_is_complete(self):
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(config["hooks"]),
            {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop"},
        )
        for event in ("SessionStart", "UserPromptSubmit"):
            command_hook = config["hooks"][event][0]["hooks"][0]
            self.assertEqual(command_hook["additionalContextLimit"], 512)
        for entries in config["hooks"].values():
            command = entries[0]["hooks"][0]["command"]
            self.assertIn("/smart-router", command)
            self.assertIn("$router_base/runtime-current", command)
            self.assertIn("$router_link/hooks/router_hook.py", command)
            self.assertIn("plugin_runtime", command)
            self.assertIn("runtime-releases/$router_suffix", command)
            self.assertIn("else exit 0", command)

    def test_hook_uses_stable_runtime_when_versioned_cache_disappears(self):
        stable_home = self.data / "stable-runtime-home"
        errors, _ = install_agents.install(stable_home, apply=True)
        self.assertEqual(errors, 0)
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(stable_home),
                "PLUGIN_DATA": str(self.data / "stable-plugin-data"),
                "PLUGIN_ROOT": str(self.data / "deleted-version-cache"),
            }
        )
        result = subprocess.run(
            ["sh", "-c", command],
            input=json.dumps({"session_id": "stable-runtime-session", "prompt": "$router-control 状态"}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("智能路由", output["hookSpecificOutput"]["additionalContext"])

    def test_hook_fails_open_when_stable_and_plugin_runtime_are_missing(self):
        empty_home = self.data / "empty-runtime-home"
        empty_home.mkdir()
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.update({"CODEX_HOME": str(empty_home), "PLUGIN_ROOT": str(self.data / "missing-plugin")})
        result = subprocess.run(
            ["sh", "-c", command],
            input=json.dumps({"session_id": "missing", "prompt": "hello"}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_hook_fails_open_when_stable_python_returns_nonzero(self):
        stable_home = self.data / "broken-runtime-home"
        errors, _ = install_agents.install(stable_home, apply=True)
        self.assertEqual(errors, 0)
        hook = stable_home / install_agents.RUNTIME_SUBDIR / "hooks" / "router_hook.py"
        hook.write_text("raise SystemExit(9)\n", encoding="utf-8")
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.update({"CODEX_HOME": str(stable_home), "PLUGIN_ROOT": str(self.data / "missing-plugin")})
        result = subprocess.run(
            ["sh", "-c", command],
            input=json.dumps({"session_id": "broken", "prompt": "hello"}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_hook_does_not_execute_unmanaged_runtime_current_directory(self):
        unmanaged_home = self.data / "unmanaged-runtime-home"
        hook = unmanaged_home / "smart-router" / "runtime-current" / "hooks" / "router_hook.py"
        hook.parent.mkdir(parents=True)
        hook.write_text("print('UNMANAGED_EXECUTED')\n", encoding="utf-8")
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.update({"CODEX_HOME": str(unmanaged_home), "PLUGIN_ROOT": str(self.data / "missing-plugin")})
        result = subprocess.run(
            ["sh", "-c", command],
            input="{}",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_hook_rejects_secondary_release_symlink(self):
        stable_home = self.data / "secondary-link-home"
        errors, _ = install_agents.install(stable_home, apply=True)
        self.assertEqual(errors, 0)
        current = stable_home / install_agents.RUNTIME_SUBDIR
        release = current.resolve()
        outside = self.data / "moved-release"
        release.rename(outside)
        release.symlink_to(outside, target_is_directory=True)
        config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        env = os.environ.copy()
        env.update({"CODEX_HOME": str(stable_home), "PLUGIN_ROOT": str(self.data / "missing-plugin")})
        result = subprocess.run(
            ["sh", "-c", command],
            input="{}",
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
