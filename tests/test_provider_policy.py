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
import configure_local_provider
import local_provider


TZ = dt.timezone(dt.timedelta(hours=8))


class ProviderPolicyTests(unittest.TestCase):
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

    def test_local_text_first_only_replaces_read_only_light_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            scout = provider_policy.resolve_executor(
                "router_scout",
                light_profile="LOCAL_TEXT_FIRST",
                env={"LOCAL_MODEL_API_KEY": "test-key"},
                home=tmp,
            )
            monitor = provider_policy.resolve_executor(
                "router_monitor",
                light_profile="LOCAL_TEXT_FIRST",
                env={"LOCAL_MODEL_API_KEY": "test-key"},
                home=tmp,
            )
            tester = provider_policy.resolve_executor(
                "router_tester",
                light_profile="LOCAL_TEXT_FIRST",
                env={"LOCAL_MODEL_API_KEY": "test-key"},
                home=tmp,
            )
        self.assertEqual(scout.executor.id, "local_scout")
        self.assertEqual(monitor.executor.id, "local_monitor")
        self.assertEqual(scout.executor.model, "deepseek-v4-flash")
        self.assertEqual(tester.executor.id, "luna_tester")

    def test_heavy_and_light_profiles_are_orthogonal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            environment = {
                "LOCAL_MODEL_API_KEY": "local-key",
                provider_policy.GLM_ENV_KEY: "glm-key",
            }
            scout = provider_policy.resolve_executor(
                "router_scout",
                "GLM_FIRST",
                light_profile="LOCAL_TEXT_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=TZ),
                env=environment,
                home=tmp,
            )
            worker = provider_policy.resolve_executor(
                "router_worker",
                "GLM_FIRST",
                light_profile="LOCAL_TEXT_FIRST",
                now=dt.datetime(2026, 8, 24, 10, 0, tzinfo=TZ),
                env=environment,
                home=tmp,
            )
        self.assertEqual(scout.executor.id, "local_scout")
        self.assertEqual(worker.executor.id, "glm_worker")

    def test_local_provider_missing_key_or_config_falls_back_to_luna(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_config = provider_policy.resolve_executor(
                "router_scout", light_profile="LOCAL_TEXT_FIRST", env={}, home=tmp
            )
            self.assertEqual(missing_config.executor.id, "luna_scout")
            self.assertEqual(missing_config.reason, "local_config_missing")
            self.local_config(tmp)
            missing_key = provider_policy.resolve_executor(
                "router_scout", light_profile="LOCAL_TEXT_FIRST", env={}, home=tmp
            )
            self.assertEqual(missing_key.executor.id, "luna_scout")
            self.assertEqual(missing_key.reason, "local_key_missing")

    def test_local_provider_config_validation_rejects_injection_and_public_http(self):
        valid = {
            "schema_version": 1,
            "provider_id": "local_text_test",
            "display_name": "Local Text",
            "base_url": "http://10.0.0.8:8000/v1",
            "model": "deepseek-v4-flash",
            "wire_api": "responses",
            "env_key": None,
            "reasoning_effort": "medium",
            "context_window": 131072,
            "allow_insecure_http": False,
            "surrogate": None,
        }
        config, reason = local_provider.validate_config(valid)
        self.assertIsNotNone(config, reason)
        for change in (
            {"provider_id": 'bad\"; sandbox_mode="danger-full-access"'},
            {"wire_api": "chat"},
            {"base_url": "http://example.com/v1"},
            {"unknown": "field"},
        ):
            with self.subTest(change=change):
                candidate = {**valid, **change}
                parsed, _ = local_provider.validate_config(candidate)
                self.assertIsNone(parsed)
        for unsafe_name in (
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONPATH",
            "PATH",
            "HOME",
            "CODEX_HOME",
            "BASH_ENV",
            "NODE_OPTIONS",
        ):
            with self.subTest(unsafe_name=unsafe_name):
                parsed, reason = local_provider.validate_config({**valid, "env_key": unsafe_name})
                self.assertIsNone(parsed)
                self.assertEqual(reason, "env_key_unsafe")

    def test_local_provider_files_must_be_private_regular_and_catalog_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.local_config(tmp)
            config_path = local_provider.config_path(tmp)
            catalog_path = local_provider.model_catalog_path(tmp)
            os.chmod(config_path, 0o644)
            self.assertEqual(local_provider.load_config(tmp)[1], "unsafe_permissions")
            os.chmod(config_path, 0o600)
            catalog_path.write_text("{}\n", encoding="utf-8")
            os.chmod(catalog_path, 0o600)
            self.assertEqual(local_provider.load_config(tmp)[1], "model_catalog_mismatch")
            self.local_config(tmp)
            config_path.unlink()
            config_path.symlink_to(catalog_path)
            self.assertEqual(local_provider.load_config(tmp)[1], "unsafe_file_type")

    def test_stale_local_failure_cannot_reopen_after_newer_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.local_config(tmp)
            available, _, health = local_provider.health_available(config, "key", 100, tmp)
            self.assertTrue(available)
            generation = int(health["generation"])
            self.assertTrue(local_provider.record_success(config, tmp, 101, generation))
            stale = local_provider.record_failure(
                config,
                "connection refused",
                "key",
                tmp,
                now_epoch=102,
                expected_generation=generation,
            )
            self.assertTrue(stale["ignored_stale_failure"])
            self.assertEqual(local_provider.read_health(tmp)["state"], "closed")

    def test_local_half_open_probe_closes_with_matching_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.local_config(tmp)
            local_provider.record_failure(config, "connection refused", "key", tmp, now_epoch=100)
            available, reason, health = local_provider.health_available(config, "key", 160, tmp)
            self.assertTrue(available)
            self.assertEqual(reason, "circuit_probe")
            self.assertTrue(
                local_provider.record_success(
                    config,
                    tmp,
                    now_epoch=161,
                    expected_generation=int(health["generation"]),
                )
            )
            self.assertEqual(local_provider.read_health(tmp)["state"], "closed")

    def test_local_circuit_is_independent_from_glm_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.local_config(tmp)
            local_provider.record_failure(config, "connection refused", "local-key", tmp, now_epoch=100)
            result = provider_policy.resolve_executor(
                "router_scout",
                light_profile="LOCAL_TEXT_FIRST",
                now=dt.datetime.fromtimestamp(101, tz=TZ),
                env={"LOCAL_MODEL_API_KEY": "local-key"},
                home=tmp,
            )
            self.assertEqual(result.executor.id, "luna_scout")
            self.assertTrue(result.reason.startswith("local_"))
            self.assertEqual(provider_policy.read_health(tmp)["state"], "closed")

    def test_glm_surrogate_configuration_reuses_existing_env_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = configure_local_provider.glm_surrogate_config()
            local_provider.write_config(config, tmp)
            loaded, reason = local_provider.load_config(tmp)
            self.assertEqual(reason, "configured")
            self.assertEqual(loaded.env_key, provider_policy.GLM_ENV_KEY)
            self.assertEqual(loaded.model, provider_policy.GLM_MODEL)
            self.assertEqual(local_provider.config_path(tmp).stat().st_mode & 0o777, 0o600)

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
                path.write_text("LOCAL_MODEL_API_KEY=local-secret\nZHIPU_API_KEY=old\n", encoding="utf-8")
                os.chmod(path, 0o600)
                configure_glm.write_secret("new-secret")
                text = path.read_text(encoding="utf-8")
                self.assertIn("LOCAL_MODEL_API_KEY=local-secret", text)
                self.assertIn("ZHIPU_API_KEY=new-secret", text)
                self.assertNotIn("ZHIPU_API_KEY=old", text)
            finally:
                if previous is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
