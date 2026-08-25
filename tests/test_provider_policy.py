from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import provider_policy
import configure_glm


TZ = dt.timezone(dt.timedelta(hours=8))


class ProviderPolicyTests(unittest.TestCase):
    def test_peak_window_is_weekdays_only_and_end_exclusive(self):
        cases = {
            dt.datetime(2026, 8, 24, 13, 59, 59, tzinfo=TZ): False,
            dt.datetime(2026, 8, 24, 14, 0, 0, tzinfo=TZ): True,
            dt.datetime(2026, 8, 24, 17, 59, 59, tzinfo=TZ): True,
            dt.datetime(2026, 8, 24, 18, 0, 0, tzinfo=TZ): False,
            dt.datetime(2026, 8, 22, 15, 0, 0, tzinfo=TZ): False,
        }
        for moment, expected in cases.items():
            with self.subTest(moment=moment):
                self.assertEqual(provider_policy.is_peak_window(moment), expected)

    def test_luna_roles_never_switch_during_glm_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = provider_policy.resolve_executor(
                "router_scout",
                "GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 15, 0, tzinfo=TZ),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                home=tmp,
            )
        self.assertEqual(result.executor.model, "gpt-5.6-luna")
        self.assertEqual(result.reason, "luna_role")

    def test_peak_missing_key_and_images_use_terra(self):
        cases = (
            ({"now": dt.datetime(2026, 8, 24, 15, 0, tzinfo=TZ), "env": {provider_policy.GLM_ENV_KEY: "x"}}, "glm_peak_window"),
            ({"now": dt.datetime(2026, 8, 24, 10, 0, tzinfo=TZ), "env": {}}, "glm_key_missing"),
            ({"now": dt.datetime(2026, 8, 24, 10, 0, tzinfo=TZ), "env": {provider_policy.GLM_ENV_KEY: "x"}, "has_images": True}, "multimodal_requires_terra"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for kwargs, reason in cases:
                with self.subTest(reason=reason):
                    result = provider_policy.resolve_executor("router_worker", "GLM_FIRST", home=tmp, **kwargs)
                    self.assertEqual(result.executor.model, "gpt-5.6-terra")
                    self.assertEqual(result.reason, reason)

    def test_quota_circuit_uses_next_flush_time_and_then_half_opens(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = provider_policy.record_glm_failure(
                '{"code":1317,"next_flush_time":1800000000}',
                "test-key",
                tmp,
                now_epoch=1700000000,
            )
            self.assertEqual(state["retry_after"], 1800000000)
            available, reason, _ = provider_policy.glm_health_available("test-key", 1700000001, tmp)
            self.assertFalse(available)
            self.assertEqual(reason, "quota_7d")
            available, reason, _ = provider_policy.glm_health_available("test-key", 1800000000, tmp)
            self.assertTrue(available)
            self.assertEqual(reason, "circuit_probe")
            available, reason, _ = provider_policy.glm_health_available("test-key", 1800000001, tmp)
            self.assertFalse(available)
            self.assertEqual(reason, "probe_in_progress")

    def test_key_change_clears_authentication_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider_policy.record_glm_failure('{"code":1000}', "old-key", tmp, now_epoch=100)
            available, _, _ = provider_policy.glm_health_available("old-key", 101, tmp)
            self.assertFalse(available)
            available, reason, _ = provider_policy.glm_health_available("new-key", 102, tmp)
            self.assertTrue(available)
            self.assertEqual(reason, "key_changed")

    def test_transport_disconnect_opens_short_transient_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = provider_policy.record_glm_failure(
                "stream disconnected before completion: stream closed before response.completed",
                "test-key",
                tmp,
                now_epoch=100,
            )
            self.assertEqual(state["reason"], "transient")
            self.assertEqual(state["retry_after"], 220)
            available, reason, _ = provider_policy.glm_health_available("test-key", 101, tmp)
            self.assertFalse(available)
            self.assertEqual(reason, "transient")

    def test_textual_unauthorized_opens_until_key_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = provider_policy.record_glm_failure(
                "HTTP 401 Unauthorized: invalid API key",
                "test-key",
                tmp,
                now_epoch=100,
            )
            self.assertEqual(state["reason"], "authentication")
            self.assertIsNone(state["retry_after"])

    def test_secret_file_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = provider_policy.secret_path(home)
            path.parent.mkdir(parents=True)
            path.write_text("ZHIPU_API_KEY=private\n", encoding="utf-8")
            os.chmod(path, 0o644)
            self.assertIsNone(provider_policy.glm_key({}, home))
            os.chmod(path, 0o600)
            self.assertEqual(provider_policy.glm_key({}, home), "private")

    def test_policy_override_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = provider_policy.policy_path(tmp)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"peak_weekdays": [6], "peak_start": "12:00", "peak_end": "13:00"}), encoding="utf-8")
            policy = provider_policy.load_policy(tmp)
            self.assertEqual(policy["peak_weekdays"], [6])
            self.assertEqual(policy["peak_start"], "12:00")

    def test_invalid_policy_fields_fail_closed(self):
        invalid_values = (
            {"timezone": "Mars/Olympus"},
            {"peak_weekdays": []},
            {"peak_weekdays": [True]},
            {"peak_start": "18:00", "peak_end": "14:00"},
            {"transient_cooldown_seconds": 0},
            {"unknown_field": 1},
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = provider_policy.policy_path(tmp)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertTrue(provider_policy.load_policy(tmp).get("invalid"))
                result = provider_policy.resolve_executor(
                    "router_reviewer",
                    "GLM_FIRST",
                    now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=TZ),
                    env={provider_policy.GLM_ENV_KEY: "test-key"},
                    home=tmp,
                )
                self.assertEqual(result.executor.model, "gpt-5.6-terra")
                self.assertEqual(result.reason, "invalid_policy")

    def test_stale_success_cannot_close_a_newer_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            initial = provider_policy.resolve_executor(
                "router_reviewer",
                "GLM_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=TZ),
                env={provider_policy.GLM_ENV_KEY: "test-key"},
                home=tmp,
            )
            self.assertEqual(initial.health_generation, 0)
            failed = provider_policy.record_glm_failure(
                '{"code":1317,"next_flush_time":1800000000}',
                "test-key",
                tmp,
                now_epoch=1700000000,
            )
            self.assertEqual(failed["generation"], 1)
            closed = provider_policy.record_glm_success(
                tmp,
                now_epoch=1700000001,
                expected_generation=initial.health_generation,
            )
            self.assertFalse(closed)
            health = provider_policy.read_health(tmp)
            self.assertEqual(health["state"], "open")
            self.assertEqual(health["reason"], "quota_7d")

    def test_current_half_open_probe_can_close_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider_policy.record_glm_failure(
                '{"code":1317,"next_flush_time":1800000000}',
                "test-key",
                tmp,
                now_epoch=1700000000,
            )
            available, reason, health = provider_policy.glm_health_available("test-key", 1800000000, tmp)
            self.assertTrue(available)
            self.assertEqual(reason, "circuit_probe")
            self.assertTrue(
                provider_policy.record_glm_success(
                    tmp,
                    now_epoch=1800000001,
                    expected_generation=int(health["generation"]),
                )
            )
            self.assertEqual(provider_policy.read_health(tmp)["state"], "closed")

    def test_configure_glm_writes_private_atomic_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = tmp
            try:
                configure_glm.write_secret("test-secret")
                path = provider_policy.secret_path(tmp)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(provider_policy.glm_key({}, tmp), "test-secret")
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
