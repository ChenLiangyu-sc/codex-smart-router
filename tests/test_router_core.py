from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import router_core


class RouterCoreTests(unittest.TestCase):
    def test_control_commands(self):
        cases = {
            "$router-control 开启": "ON",
            "/router on": "ON",
            "$router-control 影子模式": "SHADOW",
            "/router off": "OFF",
            "$router-control 状态": "STATUS",
            "$router-control 帮助": "HELP",
            "$router-control 开启。": "ON",
            "$router-control glm 开启": "GLM_ON",
            "/router glm on": "GLM_ON",
            "$router-control glm 关闭": "GLM_OFF",
        }
        for prompt, expected in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(router_core.parse_control(prompt), expected)
        self.assertIsNone(router_core.parse_control("请帮我实现一个普通功能"))
        for prompt in (
            "不要开启智能路由",
            "如何开启智能路由？",
            "请解释“开启智能路由”是什么意思，不要执行",
            "不要关闭智能路由",
            "如何关闭智能路由？",
            "请审查 $router-control 开启 这条命令，但不要执行",
        ):
            with self.subTest(adversarial_control=prompt):
                self.assertIsNone(router_core.parse_control(prompt))

    def test_high_risk_always_stays_with_sol(self):
        for prompt in (
            "实现生产环境数据库迁移",
            "fix OAuth permission handling",
            "implement authentication and authorization checks",
            "实现用户授权和权限控制",
            "实现授权功能",
            "修复授权逻辑",
            "删除支付系统里的历史账单",
            "排查并发竞态和事务一致性",
        ):
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt)
                self.assertEqual(result["decision"], "INLINE_SOL")
                self.assertEqual(result["risk"], "HIGH")
                self.assertIsNone(result["role"])
                for writer in router_core.WRITER_ROLES:
                    self.assertFalse(router_core.write_authorized_for(prompt, writer))

    def test_low_risk_categories(self):
        cases = {
            "搜索仓库并盘点相关文件": "router_scout",
            "实现这个边界清晰的小功能": "router_worker",
            "请独立做一次代码审查": "router_reviewer",
            "等待测试任务并轮询状态": "router_monitor",
            "补充单元测试用例": "router_tester",
            "更新 README 文档": "router_docs",
        }
        for prompt, role in cases.items():
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt)
                self.assertEqual(result["decision"], "DELEGATE")
                self.assertEqual(result["role"], role)
                self.assertEqual(result["risk"], "LOW")
                self.assertEqual(result["write_authorized"], role in router_core.WRITER_ROLES)

    def test_implementation_with_verification_routes_one_worker(self):
        result = router_core.classify("实现并测试这个功能")
        self.assertEqual(result["role"], "router_worker")
        self.assertTrue(result["write_authorized"])

    def test_ambiguous_and_small_tasks_stay_with_sol(self):
        self.assertEqual(router_core.classify("实现并做一次独立代码审查")["decision"], "INLINE_SOL")
        self.assertEqual(router_core.classify("解释一下这段话")["reason_codes"], ["small_task"])

    def test_negated_writer_terms_never_grant_write_access(self):
        cases = {
            "只读盘点相关文件，不要实现、不要修复、不要改代码": "router_scout",
            "请只做代码审查，不要实现，不要修复，不要改代码": "router_reviewer",
            "请检查文档，但不要更新文档，只报告结果": "router_scout",
            "只读测试盘点：查看 package.json 和测试目录": "router_scout",
            "只能查看代码，不可写入；分析如何实现这个功能": "router_scout",
            "仅查看并分析实现方案，不做任何改动": "router_scout",
            "请勿改动任何文件，只分析实现方案": "router_scout",
            "不要变更文件，只给出修复方案": "router_scout",
            "Don't edit files; propose a fix": "router_scout",
            "只分析实现方案": "router_scout",
            "请说明如何实现这个功能": "router_scout",
            "给出修复建议": "router_scout",
            "Propose a fix": "router_scout",
        }
        for prompt, role in cases.items():
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt)
                self.assertEqual(result["decision"], "DELEGATE")
                self.assertEqual(result["role"], role)
                self.assertFalse(result["write_authorized"])
                for writer in router_core.WRITER_ROLES:
                    self.assertFalse(router_core.write_authorized_for(prompt, writer))

    def test_planning_phrase_does_not_hide_a_separate_write_action(self):
        result = router_core.classify("先分析实现方案，然后实现这个边界清晰的功能")
        self.assertEqual(result["role"], "router_worker")
        self.assertTrue(result["write_authorized"])

    def test_explicit_writer_intent_is_role_scoped(self):
        cases = {
            "实现这个边界清晰的小功能": "router_worker",
            "补充单元测试用例": "router_tester",
            "更新 README 文档": "router_docs",
        }
        for prompt, role in cases.items():
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt)
                self.assertEqual(result["role"], role)
                self.assertTrue(result["write_authorized"])
                self.assertTrue(router_core.write_authorized_for(prompt, role))

    def test_bounded_file_creation_is_valid_worker_authorization(self):
        task = "在当前工作区根目录仅新建 smart-router-smoke.txt，写入 OK 后跟换行"
        result = router_core.classify(task)
        self.assertEqual(result["decision"], "DELEGATE")
        self.assertEqual(result["role"], "router_worker")
        self.assertTrue(result["write_authorized"])
        self.assertTrue(router_core.write_authorized_for(task, "router_worker"))

    def test_post_write_read_only_verification_does_not_cancel_authorization(self):
        task = (
            "仅新建 first.txt 并写入 FIRST。不要修改、创建或删除任何其他文件；"
            "完成后只读验证目标文件内容。"
        )
        result = router_core.classify(task)
        self.assertEqual(result["role"], "router_worker")
        self.assertTrue(result["write_authorized"])
        self.assertTrue(router_core.write_authorized_for(task, "router_worker"))

    def test_authorized_metadata_does_not_trigger_auth_risk_or_write_intent(self):
        metadata_only = "WRITE_AUTHORIZED=true"
        self.assertEqual(router_core.classify(metadata_only)["reason_codes"], ["small_task"])
        self.assertFalse(router_core.write_authorized_for(metadata_only, "router_worker"))

        task = "WRITE_AUTHORIZED=true。用户明确授权执行这一项低风险写操作：仅新建 second.txt 并写入 SECOND 后跟换行"
        result = router_core.classify(task)
        self.assertEqual(result["role"], "router_worker")
        self.assertEqual(result["risk"], "LOW")
        self.assertTrue(result["write_authorized"])

        generated_variant = "用户明确要求并授权：仅新建 third.txt 并写入 THIRD 后跟换行"
        result = router_core.classify(generated_variant)
        self.assertEqual(result["role"], "router_worker")
        self.assertEqual(result["risk"], "LOW")
        self.assertTrue(result["write_authorized"])

    def test_state_persistence_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(router_core.load_state(root, "s1")["mode"], "OFF")
            router_core.set_mode(root, "s1", "ON")
            self.assertEqual(router_core.load_state(root, "s1")["mode"], "ON")
            path = router_core.state_path(root, "s1")
            self.assertNotIn("s1", path.name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_old_state_is_migrated_without_losing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = router_core.default_state("s1")
            state.pop("execution_counts")
            state.pop("last_execution")
            state.pop("recent_execution_keys")
            state["schema_version"] = 1
            state["mode"] = "ON"
            router_core._atomic_json(router_core.state_path(root, "s1"), state)
            loaded = router_core.load_state(root, "s1")
            self.assertEqual(loaded["mode"], "ON")
            self.assertEqual(loaded["schema_version"], 4)
            self.assertEqual(loaded["execution_profile"], "STABLE")
            self.assertEqual(loaded["execution_counts"], {"completed": 0, "failed": 0})
            self.assertEqual(loaded["recent_execution_keys"], [])

    def test_cleanup_expired_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router_core.set_mode(root, "old", "ON")
            path = router_core.state_path(root, "old")
            old = int(time.time()) - router_core.STATE_TTL_SECONDS - 10
            os.utime(path, (old, old))
            self.assertEqual(router_core.cleanup_expired(root), 1)
            self.assertFalse(path.exists())

    def test_telemetry_omits_prompt_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router_core.append_telemetry(root, {"event": "test", "prompt_sha256": "abc"})
            raw = (root / "telemetry.jsonl").read_text(encoding="utf-8")
            record = json.loads(raw)
            self.assertNotIn("prompt", record)
            self.assertEqual((root / "telemetry.jsonl").stat().st_mode & 0o777, 0o600)

    def test_receipt_validation(self):
        good = {
            "status": "completed",
            "summary": "done",
            "findings": [],
            "evidence": ["tests passed"],
            "changed_files": [],
            "validation": ["unit tests"],
            "remaining_risks": [],
            "needs_escalation": False,
            "recommended_next_action": "integrate",
        }
        valid, errors, parsed = router_core.validate_receipt(json.dumps(good))
        self.assertTrue(valid, errors)
        self.assertEqual(parsed, good)
        valid, errors, _ = router_core.validate_receipt("```json\n{}\n```")
        self.assertFalse(valid)
        self.assertTrue(errors)

        too_long = {**good, "summary": "x" * 501}
        valid, errors, _ = router_core.validate_receipt(json.dumps(too_long))
        self.assertFalse(valid)
        self.assertIn("summary exceeds 500 characters", errors)

        too_many = {**good, "evidence": ["item"] * 7}
        valid, errors, _ = router_core.validate_receipt(json.dumps(too_many))
        self.assertFalse(valid)
        self.assertIn("evidence exceeds 6 items", errors)

        clipped = {**good, "findings": ["x" * 800]}
        valid, errors, _ = router_core.validate_receipt(json.dumps(clipped))
        self.assertFalse(valid)
        self.assertIn("findings item reaches the 800-character truncation guard", errors)

        field_fragment = {**good, "findings": ["evidence。"]}
        valid, errors, _ = router_core.validate_receipt(json.dumps(field_fragment))
        self.assertFalse(valid)
        self.assertIn("findings contains a field-name fragment", errors)

    def test_routing_context_is_compact_and_user_visible(self):
        scout = router_core.classify("搜索仓库并盘点相关文件")
        delegated = router_core.routing_context("ON", scout)
        self.assertLessEqual(len(delegated), 400)
        self.assertIn("receipt._router_meta.route_label", delegated)
        self.assertIn("路由回退：Sol（委派未完成）", delegated)

        high_risk = router_core.classify("实现生产数据库迁移并部署")
        inline = router_core.routing_context("ON", high_risk)
        self.assertLessEqual(len(inline), 180)
        self.assertIn("INLINE_SOL", inline)

        shadow = router_core.routing_context("SHADOW", scout)
        self.assertLessEqual(len(shadow), 240)
        self.assertIn("路由预览：Luna · 只读侦察", shadow)

        worker = router_core.classify("实现这个边界清晰的小功能")
        glm_shadow = router_core.routing_context("SHADOW", worker, "GLM_FIRST")
        self.assertIn("GLM-5.3 Max / Terra", glm_shadow)

    def test_execution_profile_persists_and_glm_on_activates_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = router_core.set_execution_profile(root, "s1", "GLM_FIRST", activate=True)
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["execution_profile"], "GLM_FIRST")
            state = router_core.set_execution_profile(root, "s1", "STABLE")
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["execution_profile"], "STABLE")


if __name__ == "__main__":
    unittest.main()
