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
            "$router-control local 开启": "LOCAL_ON",
            "$router-control local 关闭": "LOCAL_OFF",
            "$router-control 经济策略 v2": "ECON_V2",
            "/router policy v2": "ECON_V2",
            "$router-control 经济策略 v1": "ECON_V1",
            "/router policy v1": "ECON_V1",
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

    def test_mixed_language_test_command_is_explicit_authorization(self):
        task = "在 backend 目录运行现有 npm test，并汇总结果"
        result = router_core.classify(task)
        self.assertEqual(result["role"], "router_tester")
        self.assertTrue(result["write_authorized"])
        self.assertTrue(router_core.write_authorized_for(task, "router_tester"))

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
            self.assertEqual(loaded["schema_version"], 7)
            self.assertIsNone(loaded["current_delegation"])
            self.assertEqual(loaded["execution_profile"], "STABLE")
            self.assertEqual(loaded["light_profile"], "LUNA_STABLE")
            self.assertEqual(loaded["economics_policy"], "V2_STATIC")
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
            "schema_version": 2,
            "objective_id": "0" * 64,
            "status": "completed",
            "summary": "done",
            "findings": [],
            "evidence": ["tests passed"],
            "evidence_manifest": [],
            "inconsistencies": [],
            "coverage": {"mode": "full", "checked": 1, "total": 1},
            "parent_verification": ["spot-check the test summary"],
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

        extra = {**good, "unbounded": "x" * 10000}
        valid, errors, _ = router_core.validate_receipt(json.dumps(extra))
        self.assertFalse(valid)
        self.assertIn("unexpected fields: unbounded", errors)

        long_manifest = {
            **good,
            "evidence_manifest": [
                {"claim": "x" * 501, "path": "a.py", "locator": "line 1", "sha256": None}
            ],
        }
        valid, errors, _ = router_core.validate_receipt(json.dumps(long_manifest))
        self.assertFalse(valid)
        self.assertIn("evidence_manifest[0].claim exceeds 500 characters", errors)

    def test_common_batch_and_complex_review_phrases_route(self):
        cases = {
            "分析所有日志": "router_scout",
            "检查 10 个日志文件": "router_scout",
            "扫描整个仓库": "router_scout",
            "合同一致性检查 8 个模块": "router_reviewer",
        }
        for prompt, role in cases.items():
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt, economics=True)
                self.assertEqual(result["decision"], "DELEGATE", result)
                self.assertEqual(result["role"], role)

        for prompt in ("读日志", "检查单个日志文件", "跨文件复核两个模块"):
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt, economics=True)
                self.assertEqual(result["decision"], "INLINE_SOL", result)
                self.assertTrue(
                    any(code.startswith(("hard_inline:", "static_break_even_proxy:")) for code in result["reason_codes"]),
                    result,
                )

    def test_v2_static_uses_tool_fast_paths_and_weak_terms_do_not_force_delegation(self):
        tool_cases = {
            "运行现有 pytest 测试": "test_command",
            "查看 git status": "git_status",
            "确认路径 /tmp/example/config.toml 是否存在": "path_exists",
            "统计当前目录的文件数量": "metadata",
        }
        for prompt, kind in tool_cases.items():
            with self.subTest(prompt=prompt):
                result = router_core.classify(prompt, economics=True)
                self.assertEqual(result["decision"], "TOOL_ONLY", result)
                self.assertEqual(result["gate_features"]["deterministic_tool_kind"], kind)

        for prompt in (
            "扫描这个仓库，看看 manifest",
            "检查多个路径里的配置",
            "请独立做一次代码审查",
            "实现这个单文件小修改",
        ):
            with self.subTest(weak_signal=prompt):
                self.assertEqual(router_core.classify(prompt, economics=True)["decision"], "INLINE_SOL")

    def test_v2_static_profile_thresholds_and_read_only_coalescing(self):
        stable = router_core.classify("批量盘点 4 个日志文件", economics=True)
        self.assertEqual(stable["decision"], "DELEGATE", stable)
        self.assertTrue(stable["gate_features"]["coalesce_candidate"])

        local_small = router_core.classify(
            "批量盘点 4 个日志文件",
            economics=True,
            light_profile="LOCAL_TEXT_FIRST",
        )
        self.assertEqual(local_small["decision"], "INLINE_SOL", local_small)
        local_large = router_core.classify(
            "批量盘点 8 个日志文件",
            economics=True,
            light_profile="LOCAL_TEXT_FIRST",
        )
        self.assertEqual(local_large["decision"], "DELEGATE", local_large)

        glm_small = router_core.classify(
            "合同一致性检查 4 个模块",
            economics=True,
            execution_profile="GLM_FIRST",
        )
        self.assertEqual(glm_small["decision"], "INLINE_SOL", glm_small)
        glm_large = router_core.classify(
            "合同一致性检查 5 个模块",
            economics=True,
            execution_profile="GLM_FIRST",
        )
        self.assertEqual(glm_large["decision"], "DELEGATE", glm_large)

    def test_v2_static_does_not_double_count_basenames_inside_paths(self):
        result = router_core.classify(
            "代码审查 4 个文件： src/a.py src/b.py src/c.py src/d.py",
            economics=True,
        )
        self.assertEqual(result["decision"], "DELEGATE", result)
        self.assertEqual(result["gate_features"]["unique_path_count"], 4)
        self.assertEqual(result["gate_features"]["independent_item_count_estimate"], 4)

    def test_v2_multimodal_semantics_are_capability_routed(self):
        result = router_core.classify("分析截图内容并列出视觉缺陷", economics=True)
        self.assertEqual(result["decision"], "DELEGATE", result)
        self.assertEqual(result["role"], "router_reviewer")
        self.assertIn("gate:multimodal_capability", result["reason_codes"])
        metadata = router_core.classify("读取图片尺寸和 EXIF", economics=True)
        self.assertEqual(metadata["decision"], "TOOL_ONLY", metadata)
        docs_write = router_core.classify(
            "读取截图内容并更新 4 份文档：a.md b.md c.md d.md",
            economics=True,
        )
        self.assertEqual(docs_write["decision"], "DELEGATE", docs_write)
        self.assertEqual(docs_write["role"], "router_worker")
        self.assertTrue(docs_write["write_authorized"])
        visual_test = router_core.classify(
            "分析截图内容并运行现有测试",
            economics=True,
        )
        self.assertEqual(visual_test["decision"], "DELEGATE", visual_test)
        self.assertEqual(visual_test["role"], "router_worker")
        self.assertTrue(visual_test["gate_features"]["semantic_multimodal"])

    def test_destructive_image_or_file_actions_always_stay_with_sol(self):
        for prompt in (
            "删除 /tmp/photos 下的重复图片",
            "把重复图像删掉，只保留一份",
            "delete duplicate images from assets/photos",
            "remove these files after comparing their image content",
            "实现脚本批量清理 assets/images 下 4 个重复 PNG",
            "把 assets/images 下 4 个重复 PNG 删除",
        ):
            with self.subTest(prompt=prompt):
                decision = router_core.classify(prompt, economics=True)
                self.assertEqual(decision["decision"], "INLINE_SOL")
                self.assertEqual(decision["risk"], "HIGH")
                self.assertIn("high_risk:destructive", decision["reason_codes"])

        safe_inventory = router_core.classify(
            "统计图片 sha256 并列出重复项，不要删除任何文件",
            economics=True,
        )
        self.assertEqual(safe_inventory["decision"], "TOOL_ONLY")
        self.assertEqual(safe_inventory["risk"], "LOW")

    def test_v1_compat_preserves_work_units_gate(self):
        prompt = "跨文件复核两个模块"
        v2 = router_core.classify(prompt, economics=True)
        v1 = router_core.classify(prompt, economics=True, economics_policy="V1_COMPAT")
        self.assertEqual(v2["decision"], "INLINE_SOL")
        self.assertEqual(v1["decision"], "DELEGATE")
        self.assertIn("policy:v1_compat", v1["reason_codes"])

    def test_runtime_lease_is_task_bound_and_one_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = "lease-session"
            router_core.set_mode(root, sid, "ON")
            state = router_core.load_state(root, sid)
            args = {"task": "搜索仓库所有文件"}
            digest = router_core.delegation_task_digest("route_task", args)
            state["last_decision"] = {
                "decision": "DELEGATE",
                "decision_id": "1" * 64,
                "lease_id": "2" * 32,
                "role": "router_scout",
            }
            state["current_delegation"] = {
                "decision_id": "1" * 64,
                "lease_id": "2" * 32,
                "role": "router_scout",
                "task_digest": digest,
                "status": "started",
            }
            router_core.save_state(root, sid, state)
            self.assertFalse(
                router_core.consume_runtime_lease(
                    root, "1" * 64, "2" * 32, "router_scout", "0" * 64
                )
            )
            self.assertTrue(
                router_core.consume_runtime_lease(
                    root, "1" * 64, "2" * 32, "router_scout", digest
                )
            )
            self.assertFalse(
                router_core.consume_runtime_lease(
                    root, "1" * 64, "2" * 32, "router_scout", digest
                )
            )

    def test_routing_context_is_compact_and_user_visible(self):
        scout = router_core.classify("搜索仓库并盘点相关文件")
        scout["decision_id"] = "0" * 64
        delegated = router_core.routing_context("ON", scout)
        self.assertLessEqual(len(delegated), 512)
        self.assertIn("receipt._router_meta.route_label", delegated)
        self.assertIn("路由回退：Sol（委派未完成）", delegated)

        high_risk = router_core.classify("实现生产数据库迁移并部署")
        inline = router_core.routing_context("ON", high_risk)
        self.assertLessEqual(len(inline), 180)
        self.assertIn("INLINE_SOL", inline)

        tool_only = router_core.classify("查看 git status", economics=True)
        tool_context = router_core.routing_context("ON", tool_only)
        self.assertIn("TOOL_ONLY", tool_context)
        self.assertIn("do not spawn", tool_context)

        shadow = router_core.routing_context("SHADOW", scout)
        self.assertLessEqual(len(shadow), 240)
        self.assertIn("路由预览：Luna · 只读侦察", shadow)

        worker = router_core.classify("实现这个边界清晰的小功能")
        worker["decision_id"] = "0" * 64
        glm_shadow = router_core.routing_context("SHADOW", worker, "GLM_FIRST")
        self.assertIn("GLM-5.3 Max / Terra", glm_shadow)
        tool_shadow = router_core.routing_context("SHADOW", tool_only)
        self.assertIn("确定性工具 fast path", tool_shadow)

        for prompt in ("批量盘点 8 个日志文件", "分析截图内容并列出视觉缺陷"):
            with self.subTest(compact_variant=prompt):
                variant = router_core.classify(prompt, economics=True)
                variant["decision_id"] = "0" * 64
                variant["lease_id"] = "1" * 32
                self.assertLessEqual(len(router_core.routing_context("ON", variant)), 512)

    def test_routing_economics_keeps_tightly_coupled_work_inline(self):
        small = router_core.classify("请独立做一次代码审查", economics=True)
        self.assertEqual(small["decision"], "INLINE_SOL")
        self.assertIn("hard_inline:micro_task", small["reason_codes"])

        batch = router_core.classify("搜索当前仓库并批量盘点所有日志和 manifest", economics=True)
        self.assertEqual(batch["decision"], "DELEGATE")
        self.assertLessEqual(batch["estimated_parent_review_ratio"], 0.3)

    def test_execution_profile_persists_and_glm_on_activates_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = router_core.set_execution_profile(root, "s1", "GLM_FIRST", activate=True)
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["execution_profile"], "GLM_FIRST")
            state = router_core.set_execution_profile(root, "s1", "STABLE")
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["execution_profile"], "STABLE")

    def test_light_profile_persists_and_local_on_activates_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = router_core.set_light_profile(root, "s1", "LOCAL_TEXT_FIRST", activate=True)
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["light_profile"], "LOCAL_TEXT_FIRST")
            state = router_core.set_light_profile(root, "s1", "LUNA_STABLE")
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["light_profile"], "LUNA_STABLE")

    def test_economics_policy_persists_without_changing_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router_core.set_mode(root, "s1", "ON")
            state = router_core.set_economics_policy(root, "s1", "V1_COMPAT")
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(state["economics_policy"], "V1_COMPAT")
            state = router_core.set_economics_policy(root, "s1", "V2_STATIC")
            self.assertEqual(state["mode"], "ON")
            self.assertEqual(router_core.load_state(root, "s1")["economics_policy"], "V2_STATIC")


if __name__ == "__main__":
    unittest.main()
