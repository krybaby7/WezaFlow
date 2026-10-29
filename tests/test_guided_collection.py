import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
import wave
from contextlib import redirect_stdout
from unittest.mock import patch

from services.evaluation import collect


class GuidedCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'corpus'
        self.captures = []
        self.plays = []

    def record(self, path, seconds):
        self.captures.append(seconds)
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b'\x00\x00' * 160)

    def run_guided(self, answers, **kwargs):
        from services.evaluation import guided
        iterator = iter(answers)
        def ask(prompt):
            try:
                answer = next(iterator)
            except StopIteration:
                self.fail('Unexpected question: ' + prompt)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        with redirect_stdout(io.StringIO()):
            return guided.run(self.root, input_fn=ask, recorder=self.record,
                              player=self.plays.append, **kwargs)

    def review_answers(self, verbatim='actual, no, different words', final='Different words.'):
        return ['no', verbatim, final, 'EN', 'yes']

    def saved(self):
        return collect._load(self.root / 'collection.json')[1]

    def test_complete_selected_export_uses_actual_labels_and_confirmation(self):
        self.run_guided(['record', 'yes', *self.review_answers(), 'yes'], ids=['en-01'])
        self.assertEqual([12], self.captures)
        data = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(['en-01'], [s['id'] for s in data['samples']])
        self.assertEqual('actual, no, different words', data['samples'][0]['verbatim'])
        self.assertEqual(['Different words.'], data['samples'][0]['acceptable_final_texts'])
        self.assertEqual([], self.plays)

    def test_default_six_can_all_skip_without_capture_or_export(self):
        from services.evaluation import guided
        self.assertEqual(('en-01', 'en-06', 'fr-01', 'fr-06', 'ar-01', 'ar-06'), guided.DEFAULT_IDS)
        self.run_guided(['skip'] * 6)
        self.assertEqual([], self.captures)
        self.assertFalse((self.root / 'manifest.json').exists())

    def test_decline_capture_and_quit_are_inert(self):
        self.run_guided(['record', 'no', 'quit'], ids=['en-01', 'fr-01'])
        self.assertEqual([], self.captures)

    def test_resume_unreviewed_audio_optional_playback_and_confirmed_skip(self):
        path = collect.initialize(self.root)
        collect.record(path, 'en-01', 1, input_fn=lambda _: 'yes', recorder=self.record)
        self.captures.clear()
        answers = self.review_answers('réserve احجز', 'Réserve احجز.')
        answers[0] = 'yes'
        self.run_guided(['review', *answers, 'no'], ids=['en-01'])
        self.assertEqual([], self.captures)
        self.assertEqual([self.root / 'audio/en-01.wav'], self.plays)
        self.run_guided(['yes'], ids=['en-01'])
        self.assertTrue((self.root / 'manifest.json').exists())

    def test_blank_or_declined_review_preserves_audio_and_no_export(self):
        for tail in (['no', ''], [*self.review_answers()[:-1], 'no']):
            with self.subTest(tail=tail):
                if not (self.root / 'collection.json').exists():
                    self.run_guided(['record', 'yes', *tail], ids=['en-01'])
                else:
                    self.run_guided(['review', *tail], ids=['en-01'])
                sample = self.saved()['samples'][0]
                self.assertIn('audio', sample)
                self.assertNotIn('annotation_confirmed', sample)
                self.assertFalse((self.root / 'manifest.json').exists())

    def test_validation_precedes_capture(self):
        for options in ({'ids': []}, {'ids': ['en-01', 'bad']},
                        {'ids': ['en-01', 'en-01']}, {'seconds': float('nan')},
                        {'seconds': 121}, {'export_filename': '../outside.json'},
                        {'export_filename': 'collection.json'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_guided([], **options)
        self.assertEqual([], self.captures)

    def test_existing_export_rejected_before_capture(self):
        self.root.mkdir()
        (self.root / 'manifest.json').write_text('existing')
        with self.assertRaises(ValueError):
            self.run_guided([], ids=['en-01'])
        self.assertEqual([], self.captures)

    def test_tampered_or_escaping_audio_rejected_before_player_and_annotation(self):
        path = collect.initialize(self.root)
        collect.record(path, 'en-01', 1, input_fn=lambda _: 'yes', recorder=self.record)
        original = self.saved()
        for audio in ({'path': '../outside.wav', 'sha256': '0' * 64},
                      {**original['samples'][0]['audio'], 'sha256': '0' * 64}):
            original['samples'][0]['audio'] = audio
            path.write_text(json.dumps(original), encoding='utf-8')
            with self.assertRaises(ValueError):
                self.run_guided(['review'], ids=['en-01'])
        self.assertEqual([], self.plays)

    def test_matching_hash_does_not_make_invalid_wav_playable(self):
        path = collect.initialize(self.root)
        collect.record(path, 'en-01', 1, input_fn=lambda _: 'yes', recorder=self.record)
        audio = self.root / 'audio/en-01.wav'
        audio.write_bytes(b'not WAV')
        data = self.saved()
        data['samples'][0]['audio']['sha256'] = hashlib.sha256(audio.read_bytes()).hexdigest()
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.run_guided(['review'], ids=['en-01'])
        self.assertEqual([], self.plays)

    def test_oversized_matching_hash_wav_rejected_before_read_or_play(self):
        from services.evaluation import guided
        path = collect.initialize(self.root)
        collect.record(path, 'en-01', 1, input_fn=lambda _: 'yes', recorder=self.record)
        audio = self.root / 'audio/en-01.wav'
        with audio.open('r+b') as stream:
            stream.truncate(guided.MAX_WAV_BYTES + 1)
        data = self.saved()
        data['samples'][0]['audio']['sha256'] = hashlib.sha256(audio.read_bytes()).hexdigest()
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'oversized'):
            self.run_guided(['review'], ids=['en-01'])
        self.assertEqual([], self.plays)

    def test_language_tags_corrected_and_exact_unicode_labels_retained(self):
        answers = self.review_answers('انا قلت réserve', 'Réserve 3ayez.')
        answers[-2] = 'AR, FR, mixed'
        self.run_guided(['record', 'yes', *answers, 'no'], ids=['ar-06'])
        sample = next(s for s in self.saved()['samples'] if s['id'] == 'ar-06')
        self.assertEqual('انا قلت réserve', sample['verbatim'])
        self.assertEqual(['Réserve 3ayez.'], sample['acceptable_final_texts'])
        self.assertEqual(['AR', 'FR', 'mixed'], sample['language_tags'])

    def test_skipped_selected_sample_prevents_partial_export(self):
        self.run_guided(['record', 'yes', *self.review_answers(), 'skip'], ids=['en-01', 'fr-01'])
        self.assertTrue(self.saved()['samples'][0]['annotation_confirmed'])
        self.assertFalse((self.root / 'manifest.json').exists())

    def test_quit_preserves_earlier_confirmed_sample(self):
        self.run_guided(['record', 'yes', *self.review_answers(), 'quit'], ids=['en-01', 'fr-01'])
        self.assertTrue(self.saved()['samples'][0]['annotation_confirmed'])
        self.assertTrue((self.root / 'audio/en-01.wav').exists())
        self.assertFalse((self.root / 'manifest.json').exists())

    def test_capture_failure_keeps_no_partial_audio_or_metadata(self):
        from services.evaluation import guided
        def failed_record(path, seconds):
            Path(path).write_bytes(b'partial')
            raise RuntimeError('injected capture failure')
        answers = iter(['record', 'yes'])
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'capture failure'):
            guided.run(self.root, ids=['en-01'], input_fn=lambda _: next(answers), recorder=failed_record)
        self.assertNotIn('audio', self.saved()['samples'][0])
        self.assertFalse((self.root / 'audio/en-01.wav').exists())

    def test_resume_after_record_metadata_commit_then_interrupt(self):
        real_write = collect._write
        path = collect.initialize(self.root)
        def commit_then_interrupt(*args):
            real_write(*args)
            raise KeyboardInterrupt()
        with patch.object(collect, '_write', side_effect=commit_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                collect.record(path, 'en-01', 12, input_fn=lambda _: 'yes', recorder=self.record)
        self.captures.clear()
        self.run_guided(['review', *self.review_answers(), 'yes'], ids=['en-01'])
        self.assertEqual([], self.captures)
        self.assertTrue((self.root / 'manifest.json').exists())

    def test_windows_player_disables_default_beep(self):
        from services.evaluation import guided
        calls = []
        fake = types.SimpleNamespace(SND_FILENAME=1, SND_NODEFAULT=2,
                                     PlaySound=lambda *args: calls.append(args))
        with patch.dict(sys.modules, {'winsound': fake}):
            guided._play(Path('local.wav'))
        self.assertEqual([('local.wav', 3)], calls)

    def test_cli_interrupts_preserve_recording_and_do_not_export(self):
        from services.evaluation import guided
        for exception in (EOFError(), KeyboardInterrupt()):
            with self.subTest(exception=type(exception).__name__):
                answers = iter(['record', 'yes', exception] if not (self.root / 'collection.json').exists()
                               else ['review', exception])
                def ask(_):
                    result = next(answers)
                    if isinstance(result, BaseException):
                        raise result
                    return result
                with patch('builtins.input', ask), patch.object(collect, 'record', wraps=collect.record) as recording:
                    # Inject only the recorder while retaining the real record/confirmation semantics.
                    real_record = recording._mock_wraps
                    recording.side_effect = lambda *a, **kw: real_record(*a, **kw, recorder=self.record)
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(130, guided.main(['--output-dir', str(self.root), '--id', 'en-01']))
                self.assertIn('audio', self.saved()['samples'][0])
                self.assertFalse((self.root / 'manifest.json').exists())

    def test_interrupt_after_export_preserves_manifest_and_reports_uncertainty(self):
        from services.evaluation import guided
        self.run_guided(['record', 'yes', *self.review_answers(), 'no'], ids=['en-01'])
        real_export = collect.export
        def export_then_interrupt(*args, **kwargs):
            real_export(*args, **kwargs)
            raise KeyboardInterrupt()
        output = io.StringIO()
        with patch('builtins.input', return_value='yes'), patch.object(collect, 'export', side_effect=export_then_interrupt), redirect_stdout(output):
            self.assertEqual(130, guided.main(['--output-dir', str(self.root), '--id', 'en-01']))
        self.assertTrue((self.root / 'manifest.json').exists())
        self.assertIn('check', output.getvalue().lower())
        self.assertNotIn('No manifest exported', output.getvalue())

    def test_review_displays_verified_audio_path(self):
        from services.evaluation import guided
        path = collect.initialize(self.root)
        collect.record(path, 'en-01', 1, input_fn=lambda _: 'yes', recorder=self.record)
        answers = iter(['review', 'no', ''])
        output = io.StringIO()
        with redirect_stdout(output):
            guided.run(self.root, ids=['en-01'], input_fn=lambda _: next(answers))
        self.assertIn(str(self.root / 'audio/en-01.wav'), output.getvalue())

    def test_fresh_import_help_and_utf8_cli_are_inert(self):
        script = "import sys; import services.evaluation.guided; assert 'winsound' not in sys.modules; assert 'services.runtime.audio_smoke' not in sys.modules"
        result = subprocess.run([sys.executable, '-c', script], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        result = subprocess.run([sys.executable, '-m', 'services.evaluation.guided', '--help'], capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        result = subprocess.run([sys.executable, '-m', 'services.evaluation.guided', '--output-dir', str(self.root), '--id', 'fr-01', '--id', 'ar-01'], input=b'skip\nskip\n', capture_output=True, env={**os.environ, 'PYTHONIOENCODING': 'cp1252'})
        self.assertEqual(0, result.returncode, result.stderr)
        output = result.stdout.decode('utf-8')
        self.assertIn('Réserve', output)
        self.assertIn('احجز', output)


if __name__ == '__main__':
    unittest.main()

