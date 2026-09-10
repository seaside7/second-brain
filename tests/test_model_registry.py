"""Tests for the model registry (model_router.py) — resolve order, settings
persistence, validation, bulk apply, audit, and execute() short-circuits.

Isolation: settings_path and the audit log are pointed at a temp dir, so the
real per-workspace state files are never touched and no live API calls are
made (the execute() cases that run go through validation-only short-circuits).

Run: python -m unittest discover tests   (stdlib unittest - no framework here)
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".agent" / "scripts"))
import model_router as mr


def _fake_settings_path(workspace="personal"):
    ws = workspace if workspace in mr.KNOWN_WORKSPACES else "personal"
    return Path(_tmp) / ws / "model_settings.json"


class ModelRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _tmp
        _tmp = tempfile.mkdtemp(prefix="modrouter_test_")
        cls.tmp = Path(_tmp)
        cls.settings_patcher = patch.object(mr, "settings_path", new=_fake_settings_path)
        cls.audit_patcher = patch.object(mr, "MODEL_AUDIT_LOG", cls.tmp / "model_audit.jsonl")
        cls.settings_patcher.start()
        cls.audit_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.audit_patcher.stop()
        cls.settings_patcher.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for ws in mr.KNOWN_WORKSPACES:
            mr.save_settings(ws, {"schema_version": 1, "modules": {},
                                  "workspace_defaults": {}})
        if hasattr(self, "_with_probe") or True:
            mr.MODULES.pop("__test_nodflt__", None)

    def tearDown(self):
        mr.MODULES.pop("__test_nodflt__", None)

    # ── registry shape ──

    def test_registry_has_34_modules(self):
        self.assertEqual(len(mr.MODULES), 34)
        for mid, m in mr.MODULES.items():
            self.assertTrue(m.get("label"), mid)
            self.assertIn(m.get("capability"),
                          ("text_generation", "embedding", "image",
                           "audio_transcription", "coding"))

    def test_unknown_module_resolves_to_unknown(self):
        r = mr.resolve_module("bogus_module", "personal")
        self.assertEqual(r["provider"], "unknown")
        self.assertEqual(r["source"], "none")
        self.assertFalse(r["available"])
        self.assertEqual(r.get("error"), "unknown_module")

    # ── resolution layers ──

    def test_system_default_when_no_overrides(self):
        r = mr.resolve_module("news_writer", "personal")
        self.assertEqual(r["source"], "system")
        self.assertEqual((r["provider"], r["model"]), ("openai", "gpt-5.6-luna"))
        r2 = mr.resolve_module("personal_chat", "personal")
        self.assertEqual(r2["source"], "system")
        self.assertEqual((r2["provider"], r2["model"]), ("deepseek", "deepseek-chat"))

    def test_capability_default_only_for_modules_without_purpose_default(self):
        # A module with NO built-in default falls to capability_defaults.
        mr.MODULES["__test_nodflt__"] = {
            "label": "Probe No Default", "purpose": "test probe",
            "workspace": "*", "capability": "text_generation", "emergency": False}
        self.addCleanup(mr.MODULES.pop, "__test_nodflt__", None)
        r = mr.resolve_module("__test_nodflt__", "personal")
        self.assertEqual(r["source"], "capability")
        self.assertEqual((r["provider"], r["model"]), ("deepseek", "deepseek-chat"))
        # But a purpose-default module (news_writer) keeps its own tier.
        r2 = mr.resolve_module("news_writer", "personal")
        self.assertEqual(r2["source"], "system")
        self.assertEqual((r2["provider"], r2["model"]), ("openai", "gpt-5.6-luna"))

    def test_module_override_beats_everything(self):
        mr.MODULES["__test_nodflt__"] = {
            "label": "Probe", "purpose": "probe", "workspace": "*",
            "capability": "text_generation", "emergency": False}
        self.addCleanup(mr.MODULES.pop, "__test_nodflt__", None)
        ok, detail = mr.set_module("personal", "news_writer", "deepseek",
                                   "deepseek-chat")
        self.assertTrue(ok, detail)
        r = mr.resolve_module("news_writer", "personal")
        self.assertEqual(r["source"], "module")
        self.assertEqual((r["provider"], r["model"]), ("deepseek", "deepseek-chat"))
        # direct override also beats the fallback probe module's capability path
        ok2, _ = mr.set_module("personal", "__test_nodflt__", "openai", "gpt-5.6-luna")
        self.assertTrue(ok2)
        r2 = mr.resolve_module("__test_nodflt__", "personal")
        self.assertEqual(r2["source"], "module")
        self.assertEqual((r2["provider"], r2["model"]), ("openai", "gpt-5.6-luna"))

    def test_workspace_default_layers_below_module_override(self):
        # Workspace default only applies when no module override exists.
        ok, _ = mr.set_workspace_default("personal", "text_generation",
                                         "deepseek", "deepseek-chat")
        self.assertTrue(ok)
        r = mr.resolve_module("news_writer", "personal")
        self.assertEqual(r["source"], "workspace")
        self.assertEqual((r["provider"], r["model"]), ("deepseek", "deepseek-chat"))
        mr.set_module("personal", "news_writer", "openai", "gpt-5.6-sol")
        r2 = mr.resolve_module("news_writer", "personal")
        self.assertEqual(r2["source"], "module")
        self.assertEqual((r2["provider"], r2["model"]), ("openai", "gpt-5.6-sol"))

    def test_workspace_isolation(self):
        mr.set_module("personal", "news_writer", "deepseek", "deepseek-chat")
        r = mr.resolve_module("news_writer", "samudera")
        self.assertEqual(r["source"], "system")
        self.assertEqual((r["provider"], r["model"]), ("openai", "gpt-5.6-luna"))

    def test_override_fallback_persists(self):
        ok, _ = mr.set_module("personal", "news_writer", "openai", "gpt-5.6-luna",
                              fallback_provider="deepseek", fallback_model="deepseek-chat")
        self.assertTrue(ok)
        r = mr.resolve_module("news_writer", "personal")
        self.assertEqual(r["fallback"],
                         {"provider": "deepseek", "model": "deepseek-chat"})

    # ── validation ──

    def test_set_module_validation(self):
        ok, detail = mr.set_module("personal", "nope", "deepseek", "deepseek-chat")
        self.assertFalse(ok)
        self.assertIn("unknown module", detail)
        ok, detail = mr.set_module("personal", "news_writer", "deepseek", "gpt-5.6-luna")
        self.assertFalse(ok)
        self.assertIn("not a deepseek model", detail)
        ok, detail = mr.set_module("personal", "knowledge_embeddings", "openai",
                                   "gpt-5.6-luna")
        self.assertFalse(ok)
        self.assertIn("does not support capability", detail)
        ok, detail = mr.set_module("personal", "news_writer", "mystery", "x")
        self.assertFalse(ok)
        self.assertIn("unsupported provider", detail)

    # ── reset ──

    def test_reset_module_and_reset_all(self):
        mr.set_module("personal", "news_writer", "deepseek", "deepseek-chat")
        mr.set_workspace_default("personal", "text_generation", "deepseek", "deepseek-chat")
        r = mr.reset_module("personal", "news_writer")
        self.assertTrue(r["ok"])
        self.assertEqual(mr.resolve_module("news_writer", "personal")["source"], "workspace")
        r2 = mr.reset_module("personal")
        self.assertTrue(r2["ok"])
        self.assertEqual(mr.resolve_module("news_writer", "personal")["source"], "system")
        data = mr.load_settings("personal")
        self.assertEqual(data["modules"], {})
        self.assertEqual(data["workspace_defaults"], {})
        r3 = mr.reset_module("personal", "nope")
        self.assertFalse(r3["ok"])

    # ── bulk apply ──

    def test_bulk_apply(self):
        r = mr.bulk_apply("personal", "openai", "gpt-5.6-luna")
        self.assertTrue(r["ok"])
        expected = [m for m in mr.MODULES
                    if mr.MODULES[m]["capability"] == mr.CAP_TEXT]
        self.assertEqual(sorted(r["affected"]), sorted(expected))
        bad = mr.bulk_apply("personal", "claude", "harvest")
        self.assertFalse(bad["ok"])
        self.assertIn("deepseek/openai", bad["error"])
        bad2 = mr.bulk_apply("personal", "deepseek", "gpt-5.6-luna")
        self.assertFalse(bad2["ok"])
        applied = mr.apply_bulk("personal", "deepseek", "deepseek-chat", r["affected"])
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["applied"], len(expected))
        spot = mr.resolve_module("news_writer", "personal")
        self.assertEqual((spot["provider"], spot["model"]), ("deepseek", "deepseek-chat"))
        mr.reset_module("personal")

    # ── catalog ──

    def test_catalog_for_capability_non_secret(self):
        emb = mr.catalog_for_capability("embedding", "personal")
        self.assertTrue(any(o["model"] == "text-embedding-3-small" for o in emb))
        for o in emb:
            self.assertNotIn("key", json.dumps(o))
        txt = mr.catalog_for_capability("text_generation", "personal")
        backends = {o["provider"] for o in txt}
        self.assertTrue({"deepseek", "openai", "agy", "claude"} <= backends)

    # ── execute() short circuits (no network) ──

    def test_execute_unknown_module_no_backend(self):
        ok, _text, meta = mr.execute("bogus_module", "personal", prompt="hi")
        self.assertFalse(ok)
        self.assertIn("no backend", meta["reason"])

    def test_execute_disabled_module(self):
        mr.set_module("personal", "memory_classify", "deepseek", "deepseek-chat",
                      enabled=False)
        ok, _text, meta = mr.execute("memory_classify", "personal", prompt="hi")
        self.assertFalse(ok)
        self.assertEqual(meta["reason"], "module disabled in settings")

    def test_execute_non_text_module(self):
        ok, _text, meta = mr.execute("knowledge_embeddings", "personal", prompt="hi")
        self.assertFalse(ok)
        self.assertIn("not text_generation", meta["reason"])

    # ── audit ──

    def test_audit_log_written(self):
        mr.set_module("personal", "stock_deepdive", "deepseek", "deepseek-chat")
        mr.reset_module("personal", "stock_deepdive")
        log = self.tmp / "model_audit.jsonl"
        self.assertTrue(log.exists())
        lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        actions = [l["action"] for l in lines]
        self.assertIn("set", actions)
        self.assertIn("reset", actions)
        self.assertEqual(lines[-1]["module"], "stock_deepdive")

    # ── settings round-trip ──

    def test_settings_round_trip_atomic(self):
        mr.save_settings("personal", {"schema_version": 1, "modules": {},
                                      "workspace_defaults": {}})
        p = mr.settings_path("personal")
        self.assertTrue(p.exists())
        self.assertFalse(p.with_suffix(".tmp").exists())
        self.assertEqual(mr.load_settings("personal").get("schema_version"), 1)
        self.assertEqual(mr.load_settings("notaworkspace").get("schema_version"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)