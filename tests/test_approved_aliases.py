import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.llm.constrained_editor import ConstrainedEditor
from services.pipeline.orchestrator import PipelineOrchestrator
from services.runtime.api import create_app
from services.runtime.local_editor import LocalCleanupFormatter
from services.vocabulary.store import VocabularyStore
from test_pipeline import FakeFormatter, FakeInjector, FakeTranscriber, FakeVAD, fake_context


class ApprovedAliasTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "db" / "localflow.db"
        self.db.parent.mkdir()

    def pipeline(self, store, transcript, formatter=None):
        return PipelineOrchestrator(
            vad=FakeVAD(result=[0.2, 0.4]),
            transcriber=FakeTranscriber(transcript),
            formatter=formatter or FakeFormatter(),
            context_provider=fake_context,
            injector=FakeInjector(),
            vocabulary_store=store,
        )

    def test_authenticated_api_updates_aliases_in_an_existing_database(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("CREATE TABLE vocabulary (word TEXT UNIQUE, frequency INTEGER)")
            connection.execute("INSERT INTO vocabulary VALUES ('previous term', 2)")
        app = create_app(root=self.root, engine_token="fixture-secret", engine_id="fixture-engine")
        with TestClient(app, headers={"Authorization": "Bearer fixture-secret"}) as client:
            self.assertEqual(client.get("/vocabulary/aliases").json().get("aliases"), [])
            self.assertEqual(client.post("/vocabulary/aliases", json={"spoken": 1, "canonical": "Wezza"}).status_code, 422)
            self.assertEqual(client.post("/vocabulary/aliases", json={"spoken": "WESA", "canonical": "Wessa"}).status_code, 200)
            edited = client.post("/vocabulary/aliases", json={"spoken": "wesa", "canonical": "Wezza"})
            self.assertEqual(edited.json()["aliases"], [{"spoken": "wesa", "canonical": "Wezza"}])
            terms = client.post("/vocabulary/terms", json={"word": "Wezza"}).json()["terms"]
            self.assertEqual(terms[-1]["display_word"], "Wezza")
            store = VocabularyStore(self.db)
            pipeline = self.pipeline(store, "wEsA")
            result = pipeline.process_audio([0.2]).to_dict()
            self.assertEqual(result["raw_transcript"], "wEsA")
            self.assertEqual(result["polished_text"], "polished: Wezza")
            self.assertEqual(store.list_vocabulary()[0]["word"], "previous term")
            self.assertEqual(pipeline.transcriber.calls[-1][2], "Wezza, previous term")
            self.assertEqual(client.delete("/vocabulary/aliases", params={"spoken": "WESA"}).json()["aliases"], [])
            self.assertEqual(pipeline.process_audio([0.2]).polished_text, "polished: wEsA")

    def test_replacement_is_longest_literal_unicode_aware_and_non_cascading(self):
        store = VocabularyStore(self.db)
        store.record_correction("whisper", "Unapproved")
        store.save_alias("flow", "Flow")
        store.save_alias("whisper flow", "Wispr Flow")
        store.save_alias("Wispr Flow", "Alternate")
        store.save_alias("WESA", "Wezza")
        store.save_alias("A+B", r"A\B")
        store.save_alias("strasse", "Straße")
        raw = (
            "WHISPER FLOW, wesa! اكتب WESA هنا؛ whisper. "
            "https://wesa.io/wesa wesa.io wesa.io/?name=wesa localhost:8080/wesa?name=wesa wesa_id wesa-id obj.wesa @wesa wesa@example.com"
            " (A+B) wesaَ وwesa STRAẞE"
        )
        expected = (
            "Wispr Flow, Wezza! اكتب Wezza هنا؛ whisper. "
            "https://wesa.io/wesa wesa.io wesa.io/?name=wesa localhost:8080/wesa?name=wesa wesa_id wesa-id obj.wesa @wesa wesa@example.com"
            " (A\\B) wesaَ وwesa Straße"
        )
        pipeline = self.pipeline(store, raw)
        result = pipeline.process_audio([0.2])
        self.assertEqual(result.raw_transcript, raw)
        self.assertEqual(result.polished_text, "polished: " + expected)

    def test_raw_fallback_has_exact_asr_text_with_approved_aliases(self):
        store = VocabularyStore(self.db)
        store.save_alias("WESA", "Wezza")
        raw = "  wesa.  "
        pipeline = self.pipeline(store, raw, LocalCleanupFormatter())
        result = pipeline.process_audio([0.2])
        self.assertEqual(result.polished_text, raw)
        self.assertEqual(result.raw_transcript, raw)
        self.assertEqual(result.diagnostics["editing"]["outcome"], "raw_fallback")

        class BrokenBackend:
            def edit(self, *_args, **_kwargs):
                raise TimeoutError

        failed = LocalCleanupFormatter(editor=ConstrainedEditor(BrokenBackend()))
        pipeline = self.pipeline(store, raw, failed)
        self.assertEqual(pipeline.process_audio([0.2]).polished_text, raw)

    def test_asr_prompt_respects_worker_limit_without_truncating_terms(self):
        store = VocabularyStore(self.db)
        store.save_alias("WESA", "Wezza")
        pipeline = self.pipeline(store, "wesa")
        pipeline.process_audio([0.2], vocabulary_hints=["x" * 2200, "Wispr Flow", "y" * 1985])
        self.assertEqual(pipeline.transcriber.calls[-1][2], "Wezza, Wispr Flow")

    def test_asr_keeps_canonical_punctuation_and_prioritizes_its_casing(self):
        store = VocabularyStore(self.db)
        store.add_word("wezza, inc.")
        store.save_alias("WESA", "Wezza, Inc.")
        pipeline = self.pipeline(store, "wesa")
        pipeline.process_audio([0.2])
        self.assertEqual(pipeline.transcriber.calls[-1][2], "Wezza, Inc.")
        pipeline.process_audio([0.2], vocabulary_hints=["y" * 1987])
        self.assertEqual(len(pipeline.transcriber.calls[-1][2]), 2000)

    def test_successful_unchanged_local_editor_still_uses_approved_aliases(self):
        store = VocabularyStore(self.db)
        store.save_alias("whisper flow", "Wispr Flow")
        raw = "Try whisper flow and whisper."

        class IdentityBackend:
            def edit(self, **_kwargs):
                return '{"edited_text":"Try whisper flow and whisper."}'

        formatter = LocalCleanupFormatter(editor=ConstrainedEditor(IdentityBackend()))
        result = self.pipeline(store, raw, formatter).process_audio([0.2])
        self.assertEqual(result.raw_transcript, raw)
        self.assertEqual(result.polished_text, "Try Wispr Flow and whisper.")

    def test_postpass_does_not_expand_a_result_past_its_character_budget(self):
        store = VocabularyStore(self.db)
        store.save_alias("x", "Y" * 128)
        transcript = "x " * 900
        result = self.pipeline(store, transcript).process_audio([0.2])
        self.assertEqual(result.polished_text, "polished: " + transcript)

    def test_approved_initial_case_is_per_result_and_requires_a_complete_phrase(self):
        store = VocabularyStore(self.db)
        store.save_alias("WESA", "Wezza")
        store.save_alias("whisper flow", "Wispr Flow")
        store.record_correction("words", "Words")

        class IdentityBackend:
            def edit(self, *, system_prompt, user_payload):
                payload = json.loads(user_payload)
                return json.dumps({"edited_text": payload["raw"]})

        formatter = LocalCleanupFormatter(editor=ConstrainedEditor(IdentityBackend()))
        for transcript, expected in (
            ("WESA, send this", True),
            ("Wezza", True),
            ("Wispr Flow.", True),
            ("whisper flow is open", True),
            ("WezzaFlow is open", False),
            ("Wispr Flower", False),
            ("Wezza.io", False),
            ("Wispr Flow.io", False),
            ("Ordinary text about Wezza", False),
        ):
            with self.subTest(transcript=transcript):
                result = self.pipeline(store, transcript, formatter).process_audio([0.2])
                self.assertEqual(result.diagnostics.get("preserve_initial_case", False), expected)

        self.assertIsNone(self.pipeline(store, "  ", formatter).process_audio([0.2]))
        raw = self.pipeline(store, "Wezza", LocalCleanupFormatter()).process_audio([0.2])
        self.assertFalse(raw.diagnostics.get("preserve_initial_case", False))
        store.delete_alias("WESA")
        result = self.pipeline(store, "Wezza", formatter).process_audio([0.2])
        self.assertFalse(result.diagnostics.get("preserve_initial_case", False))

    def test_approved_canonical_names_reach_formatter_from_one_alias_read(self):
        store = VocabularyStore(self.db)
        store.add_word("LocalFlow")
        store.record_correction("lf", "LocalFlow")
        store.save_alias("WESA", "Wezza")
        formatter = FakeFormatter()
        with patch.object(store, "list_aliases", wraps=store.list_aliases) as list_aliases:
            self.pipeline(store, "WESA", formatter).process_audio([0.2], vocabulary_hints=["Explicit"])
        list_aliases.assert_called_once_with()
        self.assertEqual(formatter.calls[0][2], ["Wezza", "LocalFlow", "lf -> LocalFlow", "Explicit"])
