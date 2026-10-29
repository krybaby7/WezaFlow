"""Nondefault configuration actually reaches adapters, without running them."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.config.settings import load_default_settings, load_settings
from services.runtime.api import RuntimeApiState, _create_session_adapters

class SessionAdapterSettingsTests(unittest.TestCase):
    def test_profile_defaults_and_explicit_saved_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.yaml'
            path.write_text('runtime:\n  profile: quality\n')
            settings=load_settings(path)
            self.assertEqual(settings['models']['whisper'],'distil-large-v3')
            self.assertEqual(settings['models']['whisper_cpu_threads'],6)
            path.write_text('runtime:\n  profile: quality\nmodels:\n  whisper: local-custom-model\n  whisper_cpu_threads: 3\n')
            settings=load_settings(path)
            self.assertEqual(settings['models']['whisper'],'local-custom-model')
            self.assertEqual(settings['models']['whisper_cpu_threads'],3)
    def test_nondefault_saved_audio_vad_model_injection_are_wired(self):
        settings=load_default_settings()
        settings['audio']['blocksize']=384
        settings['models'].update(whisper='local-model',whisper_cpu_threads=3,whisper_compute_type='float32')
        settings['vad'].update(threshold=.031,trim_threshold=.009,trim_padding_ms=321,min_speech_duration_ms=555)
        settings['injection'].update(paste_delay_ms=17,preserve_previous_clipboard=False)
        with tempfile.TemporaryDirectory() as folder:
            state=RuntimeApiState(root=Path(folder),log_path=Path(folder)/'runtime.jsonl',find_spec=lambda _:None)
            with patch('services.asr.audio_capture.AudioRecorder.microphone',return_value='recorder') as microphone:
                recorder,pipeline=_create_session_adapters(state,settings)
            microphone.assert_called_once_with(sample_rate=16000,blocksize=384)
            self.assertEqual(recorder,'recorder')
            self.assertEqual(pipeline.vad.threshold,.031)
            self.assertEqual(pipeline.vad.trim_threshold,.009)
            self.assertEqual(pipeline.vad.trim_padding_ms,321)
            self.assertEqual(pipeline.vad.min_duration_ms,555)
            self.assertEqual(pipeline.transcriber.model_name,'local-model')
            self.assertEqual(pipeline.transcriber.model_kwargs['cpu_threads'],3)
            self.assertEqual(pipeline.transcriber.model_kwargs['compute_type'],'float32')
            self.assertIs(pipeline.transcriber.model_kwargs['local_files_only'],True)
            self.assertIsNone(pipeline.transcriber.backend)
            self.assertEqual(pipeline.injector._paste_delay_seconds,.017)
            self.assertFalse(pipeline.injector._preserve_previous_clipboard)
            self.assertEqual(pipeline.context_provider().app_name,'unknown')
