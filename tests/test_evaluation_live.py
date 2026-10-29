import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import wave
from unittest.mock import patch

from services.evaluation import live


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.snapshot = self.root / 'model'
        self.snapshot.mkdir()
        for name in live.REQUIRED_FILES:
            (self.snapshot / name).write_bytes(b'local')
        audio = self.root / 'sample.wav'
        with wave.open(str(audio), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\0\0' * 160)
        sample = dict(id='one', split='development', audio=dict(path='sample.wav', sha256=hashlib.sha256(audio.read_bytes()).hexdigest()), verbatim='Bonjour مرحبا', acceptable_final_texts=['Bonjour مرحبا'], language_tags=['mixed'], metadata=dict(dialect='unknown', script='mixed'))
        self.data = dict(schema_version=1, samples=[sample, dict(sample, id='two')])
        self.manifest = self.root / 'manifest.json'
        self.save()
        self.now = 0
        self.loads = []

    def save(self):
        self.manifest.write_text(json.dumps(self.data), encoding='utf-8')

    def factory(self, path, **kwargs):
        self.loads.append((path, kwargs))
        self.now += 3
        owner = self
        class Model:
            def transcribe(self, audio, **options):
                owner.options = options
                def segments():
                    owner.now += 2
                    yield SimpleNamespace(text='Bonjour مرحبا', start=0., end=.01)
                return segments(), SimpleNamespace(language='zz', language_probability=.4, all_language_probs=[('zz', .4)])
        return Model()

    def run_live(self, **kwargs):
        return live.run_live(self.manifest, 'development', self.snapshot, device='cpu', compute_type='int8', cpu_threads=2, source='local/test', revision='abc123', backend_factory=self.factory, clock=lambda: self.now, **kwargs)

    def test_lazy_inference_timing_warmth_and_provenance(self):
        report = self.run_live()
        self.assertEqual(len(self.loads), 1)
        self.assertTrue(self.loads[0][1]['local_files_only'])
        self.assertEqual(self.options, dict(task='transcribe', language=None))
        self.assertEqual(report['metadata']['model_load_seconds'], 3)
        self.assertEqual([r['inference_seconds'] for r in report['rows']], [2, 2])
        self.assertEqual([r['inference_warmth'] for r in report['rows']], ['cold', 'warm'])
        self.assertEqual(report['rows'][0]['language']['detected'], 'zz')
        self.assertEqual(report['rows'][0]['raw_score']['word_error_rate'], 0)
        self.assertEqual(report['metadata']['model_revision'], 'abc123')
        self.assertEqual(len(report['metadata']['model_files']['model.bin']['sha256']), 64)
        self.assertIsNone(report['metadata']['cpu_utilization_percent'])
        self.assertIsNone(report['metadata']['energy_joules'])

    def test_invalid_inputs_never_load_model(self):
        original_data = json.dumps(self.data)
        original_audio = (self.root / 'sample.wav').read_bytes()
        for mutation in ('split', 'audio', 'unselected'):
            with self.subTest(mutation=mutation):
                self.data = json.loads(original_data)
                (self.root / 'sample.wav').write_bytes(original_audio)
                if mutation == 'split':
                    self.data['samples'][0]['split'] = 'other'
                elif mutation == 'audio':
                    (self.root / 'sample.wav').write_bytes(b'invalid')
                    for sample in self.data['samples']:
                        sample['audio']['sha256'] = hashlib.sha256(b'invalid').hexdigest()
                else:
                    self.data['samples'][1]['split'] = 'heldout'
                    self.data['samples'][1]['audio'] = dict(path='missing', sha256='0'*64)
                self.save()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_live()
                self.assertEqual(self.loads, [])

    def test_snapshot_strictness(self):
        for path in (Path('relative'), self.root / 'missing'):
            with self.assertRaises(ValueError):
                live.inspect_snapshot(path, source='test', revision='rev')
        (self.snapshot / 'tokenizer.json').unlink()
        with self.assertRaisesRegex(ValueError, 'tokenizer'):
            self.run_live()
        self.assertEqual(self.loads, [])

    def test_failed_generator_retains_partial_and_does_not_warm(self):
        def factory(*args, **kwargs):
            class Model:
                def transcribe(self, *args, **kwargs):
                    def segments():
                        yield SimpleNamespace(text='partial', start=0., end=.01)
                        raise RuntimeError('decode failed')
                    return segments(), SimpleNamespace(language=None)
            return Model()
        with patch.object(self, 'factory', factory):
            report = self.run_live()
        self.assertEqual(report['counts']['failed'], 2)
        self.assertEqual([r['inference_warmth'] for r in report['rows']], ['cold', 'cold'])
        self.assertEqual(report['rows'][0]['raw'], 'partial')
        self.assertIn('decode failed', report['rows'][0]['error'])
        self.assertEqual(report['rows'][0]['status'], 'inference_failed')
        self.assertEqual(report['rows'][1]['status'], 'not_attempted_backend_failed')
        self.assertEqual(report['rows'][1]['raw'], '')
        self.assertIsNone(report['rows'][1]['raw_score'])
        self.assertIsNone(report['rows'][1]['inference_seconds'])

    def test_requires_provenance_outside_immutable_cache(self):
        with self.assertRaisesRegex(ValueError, 'source|revision'):
            live.inspect_snapshot(self.snapshot)

    def test_cli_report_failure_nonzero(self):
        output = self.root / 'out.json'
        with patch.object(live, 'run_live', return_value=dict(counts=dict(failed=1))):
            status = live.main(['--manifest', str(self.manifest), '--split', 'heldout', '--snapshot', str(self.snapshot), '--device', 'cpu', '--compute-type', 'int8', '--cpu-threads', '2', '--source', 'local/test', '--revision', 'abc', '--output', str(output)])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.read_text())['counts']['failed'], 1)

    def test_load_failure_rows_keep_audio_duration(self):
        with patch.object(self, 'factory', side_effect=RuntimeError('no CUDA')):
            report = self.run_live()
        self.assertEqual(report['counts']['failed'], 2)
        self.assertEqual(report['rows'][0]['audio_duration_seconds'], .01)
        self.assertIsNone(report['rows'][0]['inference_seconds'])
        self.assertIn('no CUDA', report['rows'][0]['error'])

    def test_cache_provenance_is_derived_only_from_commit_snapshot(self):
        cache = self.root / 'models--owner--model' / 'snapshots' / ('a'*40)
        cache.mkdir(parents=True)
        for name in live.REQUIRED_FILES:
            (cache / name).write_bytes(b'local')
        info = live.inspect_snapshot(cache)
        self.assertEqual(info['source'], 'owner/model')
        self.assertEqual(info['revision'], 'a'*40)

    def test_empty_selected_split_and_invalid_configuration_do_not_load(self):
        for kwargs in (dict(split='heldout'), dict(cpu_threads=0), dict(device='other'), dict(compute_type='bad')):
            args = dict(split='development', device='cpu', compute_type='int8', cpu_threads=2)
            args.update(kwargs)
            with self.assertRaises(ValueError):
                live.run_live(self.manifest, snapshot=self.snapshot, source='local', revision='rev', backend_factory=self.factory, **args)
        self.assertEqual(self.loads, [])

    def test_pcm_contract_rejects_stereo_rate_width_and_truncation(self):
        original = (self.root / 'sample.wav').read_bytes()
        for channels, width, rate in ((2, 2, 16000), (1, 1, 16000), (1, 2, 8000)):
            with wave.open(str(self.root / 'sample.wav'), 'wb') as wav:
                wav.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0' * 320)
            for sample in self.data['samples']:
                sample['audio']['sha256'] = hashlib.sha256((self.root / 'sample.wav').read_bytes()).hexdigest()
            self.save()
            with self.assertRaises(RuntimeError):
                self.run_live()
        (self.root / 'sample.wav').write_bytes(original[:-1])
        for sample in self.data['samples']:
            sample['audio']['sha256'] = hashlib.sha256(original[:-1]).hexdigest()
        self.save()
        with self.assertRaisesRegex(RuntimeError, 'truncated'):
            self.run_live()
        self.assertEqual(self.loads, [])

    def test_default_backend_constructs_only_local_model(self):
        import sys
        import numpy  # Keep extension module outside sys.modules patch restoration.
        fake_package = SimpleNamespace(WhisperModel=self.factory)
        with patch.dict(sys.modules, {'faster_whisper': fake_package}):
            report = live.run_live(self.manifest, 'development', self.snapshot, device='cpu', compute_type='int8', cpu_threads=2, source='local/test', revision='rev', clock=lambda: self.now)
        self.assertEqual(report['counts']['succeeded'], 2)
        self.assertEqual(self.loads[0][0], str(self.snapshot.resolve()))
        self.assertEqual(self.loads[0][1], dict(device='cpu', compute_type='int8', cpu_threads=2, num_workers=1, local_files_only=True))

    def test_effective_decode_options_and_installed_versions_are_fingerprinted(self):
        from dataclasses import dataclass
        @dataclass
        class Options:
            beam_size: int = 5
            temperatures: tuple = (0., .2)
            condition_on_previous_text: bool = True
        original_factory = self.factory
        def factory(*args, **kwargs):
            model = original_factory(*args, **kwargs)
            transcribe = model.transcribe
            def with_options(*args, **kwargs):
                segments, info = transcribe(*args, **kwargs)
                info.transcription_options = Options()
                return segments, info
            model.transcribe = with_options
            return model
        with patch.object(self, 'factory', factory), patch.object(live.importlib.metadata, 'version', side_effect=lambda name: 'test-' + name):
            report = self.run_live()
        versions = report['metadata']['backend_versions']
        self.assertEqual(versions, {'faster-whisper': 'test-faster-whisper', 'ctranslate2': 'test-ctranslate2', 'numpy': 'test-numpy'})
        row = report['rows'][0]
        self.assertEqual(row['effective_decode_options']['beam_size'], 5)
        self.assertEqual(row['effective_decode_options']['temperatures'], [0., .2])
        payload = dict(backend_versions=versions, effective_decode_options=row['effective_decode_options'])
        expected = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        self.assertEqual(row['effective_decode_fingerprint'], expected)
        self.assertEqual(row['effective_decode_options_status'], 'available')
        self.assertEqual(report['metadata']['configuration']['backend_versions'], versions)

    def test_missing_effective_decode_options_are_explicitly_unavailable(self):
        row = self.run_live()['rows'][0]
        self.assertIsNone(row['effective_decode_options'])
        self.assertIsNone(row['effective_decode_fingerprint'])
        self.assertEqual(row['effective_decode_options_status'], 'unavailable_backend_did_not_report')

    def test_mutating_model_artifact_changes_configuration_fingerprint(self):
        before = self.run_live()
        (self.snapshot / 'model.bin').write_bytes(b'replaced local model')
        after = self.run_live()
        self.assertNotEqual(before['metadata']['configuration_fingerprint'], after['metadata']['configuration_fingerprint'])
        self.assertEqual(after['metadata']['configuration']['model_files'], after['metadata']['model_files'])

    def test_progress_phases_reveal_no_transcript_or_audio(self):
        events = []
        report = self.run_live(progress=events.append)
        self.assertEqual([e['phase'] for e in events], ['validation_started', 'validation_completed', 'model_load_started', 'model_load_completed', 'clip_started', 'inference_started', 'clip_completed', 'clip_started', 'inference_started', 'clip_completed', 'run_completed'])
        self.assertEqual(events[6]['clip_id'], 'one')
        self.assertEqual(events[6]['inference_seconds'], 2)
        self.assertTrue(events[-1]['succeeded'])
        self.assertNotIn(report['rows'][0]['raw'], json.dumps(events, ensure_ascii=False))
        self.assertTrue(all(set(e) <= {'phase', 'elapsed_seconds', 'clip_id', 'inference_seconds', 'model_load_seconds', 'succeeded'} for e in events))

    def test_cli_progress_is_opt_in_stderr_json(self):
        import contextlib
        import io
        base = ['--manifest', str(self.manifest), '--split', 'development', '--snapshot', str(self.snapshot), '--device', 'cpu', '--compute-type', 'int8', '--cpu-threads', '2', '--output', str(self.root / 'report.json')]
        def run(*args, **kwargs):
            if kwargs['progress']:
                kwargs['progress'](dict(phase='model_load_started', elapsed_seconds=0.0))
            return dict(counts=dict(failed=0))
        for enabled in (False, True):
            stderr = io.StringIO()
            with patch.object(live, 'run_live', side_effect=run), contextlib.redirect_stderr(stderr):
                self.assertEqual(live.main(base + (['--progress'] if enabled else [])), 0)
            if enabled:
                self.assertEqual(json.loads(stderr.getvalue())['phase'], 'model_load_started')
            else:
                self.assertEqual(stderr.getvalue(), '')

    def test_native_failure_invalidates_backend_and_preserves_all_rows(self):
        calls = []
        class Model:
            def transcribe(self, *args, **kwargs):
                calls.append('called')
                if len(calls) > 1:
                    raise AssertionError('unsafe native backend reuse would hang')
                raise RuntimeError('Library cublas64_12.dll is not found or cannot be loaded')
        with patch.object(self, 'factory', return_value=Model()):
            report = self.run_live()
        self.assertEqual(calls, ['called'])
        self.assertEqual(report['counts'], dict(selected=2, failed=2, succeeded=0))
        self.assertEqual(report['rows'][0]['status'], 'inference_failed')
        self.assertEqual(report['rows'][1]['status'], 'not_attempted_backend_failed')
        self.assertIsNone(report['rows'][1]['inference_seconds'])
        self.assertIsNone(report['rows'][1]['raw_score'])
        self.assertIn('prior backend failure', report['rows'][1]['error'])
        self.assertEqual(report['rows'][1]['audio_duration_seconds'], .01)

    def test_pre_inference_audio_error_does_not_invalidate_backend(self):
        from services.asr.benchmark import load_wav_samples
        reads = []
        def load(path):
            reads.append(path)
            if len(reads) == 3:  # Two complete-manifest checks, then first row decode.
                raise RuntimeError('audio disappeared after validation')
            return load_wav_samples(path)
        with patch('services.asr.benchmark.load_wav_samples', side_effect=load):
            report = self.run_live()
        self.assertEqual(report['counts'], dict(selected=2, failed=1, succeeded=1))
        self.assertEqual(report['rows'][0]['status'], 'pre_inference_failed')
        self.assertEqual(report['rows'][1]['status'], 'succeeded')
        self.assertEqual(report['rows'][1]['inference_warmth'], 'cold')
