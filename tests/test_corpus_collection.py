import io
from contextlib import redirect_stdout, redirect_stderr
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from services.evaluation import collect
from services.evaluation.manifest import load_manifest

class CorpusCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'corpus'
        self.path = collect.initialize(self.root)
        self.sid = 'en-01'

    def fake_record(self, path, seconds):
        Path(path).write_bytes(b'RIFF fake local WAV')

    def ready(self):
        collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        collect.annotate(self.path, self.sid, {'verbatim':'Book twelve, no, thirteen seats.', 'acceptable_final_texts':['Book thirteen seats.']}, input_fn=lambda _: 'yes')

    def test_init_has_thirty_draft_prompts_and_refuses_overwrite(self):
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(30, len(data['samples']))
        self.assertEqual({'EN':10, 'FR':10, 'AR':10}, Counter(s['metadata']['prompt_language'] for s in data['samples']))
        self.assertFalse(any('verbatim' in s for s in data['samples']))
        with self.assertRaises(ValueError): collect.initialize(self.root)

    def test_malformed_collection_boundaries_raise_value_error(self):
        original = json.loads(self.path.read_text(encoding='utf-8'))
        invalid = [[], None, 42, {'schema_version':1, 'samples':[42]}]
        for bad_id in ('../escape', '', None):
            row = dict(original['samples'][0], id=bad_id)
            invalid.append({'schema_version':1, 'samples':[row]})
        invalid.append({'schema_version':1, 'samples':[original['samples'][0]] * 2})
        for prompt in (None, '', [], 3):
            invalid.append({'schema_version':1, 'samples':[dict(original['samples'][0], prompt=prompt)]})
        for document in invalid:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document), encoding='utf-8')
                with self.assertRaises(ValueError):
                    collect._load(self.path)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        collect.main(['status', '--collection', str(self.path)])
                self.assertEqual(2, error.exception.code)

    def test_malformed_annotation_root_rejected_by_helper_and_cli(self):
        collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        annotation_path = self.root / 'annotation.json'
        for annotation in ([], None, 42):
            with self.subTest(annotation=annotation):
                with self.assertRaises(ValueError):
                    collect.annotate(self.path, self.sid, annotation)
                annotation_path.write_text(json.dumps(annotation), encoding='utf-8')
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        collect.main(['annotate', '--collection', str(self.path), '--id', self.sid, '--annotation', str(annotation_path)])
                self.assertEqual(2, error.exception.code)

    def test_status_tolerates_stringio_streams(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(0, collect.main(['status', '--collection', str(self.path)]))
        self.assertIn('ar-10', output.getvalue())

    def test_cli_status_uses_utf8_even_with_cp1252_environment(self):
        result = subprocess.run([sys.executable, '-m', 'services.evaluation.collect', 'status', '--collection', str(self.path)], capture_output=True, env={**os.environ, 'PYTHONIOENCODING':'cp1252'})
        self.assertEqual(0, result.returncode, result.stderr)
        output = result.stdout.decode('utf-8')
        self.assertIn('Réserve', output)
        self.assertIn('احجز', output)
        self.assertIn('ar-10', output)

    def test_declined_annotation_leaves_metadata_unchanged(self):
        collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        before = self.path.read_bytes()
        collect.annotate(self.path, self.sid, {'verbatim':'actual speech', 'acceptable_final_texts':['Actual speech.']}, input_fn=lambda _: 'no')
        self.assertEqual(before, self.path.read_bytes())

    def test_decline_does_not_capture(self):
        collect.record(self.path, self.sid, 2, input_fn=lambda _: 'no', recorder=lambda *a, **k: self.fail('captured'))
        self.assertFalse((self.root/'audio'/f'{self.sid}.wav').exists())

    def test_record_annotate_selected_export_validates(self):
        self.ready()
        output = collect.export(self.path, self.root/'manifest.json', ids=[self.sid])
        self.assertEqual('development', load_manifest(output)['samples'][0]['split'])

    def test_incomplete_export_fails(self):
        with self.assertRaises(ValueError): collect.export(self.path, self.root/'manifest.json')

    def test_existing_audio_and_traversal_protected(self):
        self.ready()
        with self.assertRaises(ValueError): collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        with self.assertRaises(ValueError): collect.record(self.path, '../escape', 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        with self.assertRaises(ValueError): collect.export(self.path, self.root.parent/'outside.json', ids=[self.sid])

    def test_missing_and_tampered_audio_fail(self):
        self.ready()
        audio=self.root/'audio'/f'{self.sid}.wav'
        audio.write_bytes(b'tampered')
        with self.assertRaises(ValueError): collect.export(self.path,self.root/'manifest.json',ids=[self.sid])
        audio.unlink()
        with self.assertRaises(ValueError): collect.export(self.path,self.root/'manifest.json',ids=[self.sid])

    def test_post_commit_interrupt_retains_referenced_audio(self):
        real_write = collect._write
        def commit_then_interrupt(*args):
            real_write(*args)
            raise KeyboardInterrupt()
        with patch.object(collect, '_write', side_effect=commit_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        sample = collect._load(self.path, self.sid)[2]
        audio = self.root / sample['audio']['path']
        self.assertTrue(audio.exists())
        import hashlib
        self.assertEqual(sample['audio']['sha256'], hashlib.sha256(audio.read_bytes()).hexdigest())

    def test_precommit_failure_removes_unreferenced_audio(self):
        original = self.path.read_bytes()
        with patch.object(collect, '_write', side_effect=OSError('before commit')):
            with self.assertRaisesRegex(OSError, 'before commit'):
                collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        self.assertEqual(original, self.path.read_bytes())
        self.assertFalse((self.root / 'audio' / f'{self.sid}.wav').exists())

    def test_uncertain_persisted_state_retains_audio_and_original_failure(self):
        def corrupt_then_fail(*args):
            self.path.write_text('{unknown state', encoding='utf-8')
            raise OSError('original write failure')
        with patch.object(collect, '_write', side_effect=corrupt_then_fail):
            with self.assertRaisesRegex(OSError, 'original write failure'):
                collect.record(self.path, self.sid, 2, input_fn=lambda _: 'yes', recorder=self.fake_record)
        self.assertTrue((self.root / 'audio' / f'{self.sid}.wav').exists())

if __name__ == '__main__': unittest.main()
