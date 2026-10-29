"""Explicit offline ASR evaluation; no desktop access, formatting, or downloads."""
import argparse
from dataclasses import asdict, is_dataclass
import importlib.metadata
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time

from .manifest import load_manifest
from .scoring import score_transcription

# Requiring tokenizer + preprocessor prevents faster-whisper's remote fallbacks.
REQUIRED_FILES = ('model.bin', 'config.json', 'tokenizer.json', 'preprocessor_config.json', 'vocabulary.json')


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def inspect_snapshot(snapshot, *, source=None, revision=None):
    """Require complete local artifacts and explicit or immutable-cache provenance."""
    snapshot = Path(snapshot)
    if not snapshot.is_absolute() or not snapshot.is_dir():
        raise ValueError('snapshot must be an absolute existing local directory')
    snapshot = snapshot.resolve()
    if bool(source) != bool(revision):
        raise ValueError('source and revision must be supplied together')
    if not source:
        cache = snapshot.parent.parent.name
        if snapshot.parent.name != 'snapshots' or not re.fullmatch(r'[0-9a-f]{40}', snapshot.name) or not cache.startswith('models--'):
            raise ValueError('source and revision required outside an immutable HF cache snapshot')
        source = cache[len('models--'):].replace('--', '/')
        revision = snapshot.name
    if not isinstance(source, str) or not source.strip() or not isinstance(revision, str) or not revision.strip():
        raise ValueError('nonempty source and revision required')
    files = {}
    for name in REQUIRED_FILES:
        artifact = snapshot / name
        if not artifact.is_file() or not artifact.stat().st_size:
            raise ValueError('missing or empty snapshot file: ' + name)
        files[name] = dict(sha256=_hash_file(artifact), bytes=artifact.stat().st_size)
    return dict(path=str(snapshot), source=source, revision=revision, files=files)


def _backend(snapshot, **kwargs):
    # Imported only after all manifest, audio, configuration, and snapshot checks.
    from faster_whisper import WhisperModel
    return WhisperModel(snapshot, **kwargs)


def _language(info):
    def finite(value):
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None
    probs = getattr(info, 'all_language_probs', None)
    return dict(detected=getattr(info, 'language', None), probability=finite(getattr(info, 'language_probability', None)), all_probabilities=None if probs is None else [[name, finite(prob)] for name, prob in probs])



def _backend_versions():
    versions = {}
    for name in ('faster-whisper', 'ctranslate2', 'numpy'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _decode_provenance(info, versions):
    options = getattr(info, 'transcription_options', None)
    if options is None or not is_dataclass(options):
        return dict(effective_decode_options=None, effective_decode_fingerprint=None,
                    effective_decode_options_status='unavailable_backend_did_not_report')
    try:
        # Round trip normalizes tuples while rejecting non-finite/unsupported values.
        effective = json.loads(json.dumps(asdict(options), allow_nan=False))
        payload = dict(backend_versions=versions, effective_decode_options=effective)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return dict(effective_decode_options=None, effective_decode_fingerprint=None,
                    effective_decode_options_status='unavailable_non_json_backend_options')
    return dict(effective_decode_options=effective, effective_decode_fingerprint=fingerprint,
                effective_decode_options_status='available')


def run_live(manifest, split, snapshot, *, device, compute_type, cpu_threads,
             source=None, revision=None, backend_factory=None, clock=time.perf_counter, progress=None):
    """Own one model and transcribe sequentially; injectable backend is for tests."""
    validation_start = clock()
    def emit(phase, **fields):
        if progress is not None:
            progress(dict(phase=phase, elapsed_seconds=clock()-validation_start, **fields))
    emit('validation_started')
    if split not in ('development', 'heldout'):
        raise ValueError('explicit development or heldout split required')
    if device not in ('cpu', 'cuda'):
        raise ValueError('device must be cpu or cuda')
    if type(cpu_threads) is not int or cpu_threads <= 0:
        raise ValueError('cpu_threads must be a positive integer')
    if compute_type not in ('default', 'auto', 'int8', 'int8_float32', 'int8_float16', 'int8_bfloat16', 'int16', 'float16', 'bfloat16', 'float32'):
        raise ValueError('invalid compute_type')
    manifest = Path(manifest).resolve()
    data = load_manifest(manifest)
    selected = [s for s in data['samples'] if s['split'] == split]
    if not selected:
        raise ValueError('no samples in selected split')
    from services.asr.benchmark import load_wav_samples
    # Validate every manifest audio before constructing the model. Keep only paths;
    # selected audio is decoded again per row to bound retained corpus memory.
    durations = {}
    for sample in data['samples']:
        _, durations[sample['id']] = load_wav_samples(manifest.parent / sample['audio']['path'])
    model_info = inspect_snapshot(snapshot, source=source, revision=revision)
    versions = _backend_versions()
    configuration = dict(backend_versions=versions, backend_factory='injected' if backend_factory else 'native', backend='faster-whisper', snapshot=model_info['path'], device=device,
                         compute_type=compute_type, cpu_threads=cpu_threads, num_workers=1,
                         task='transcribe', language=None, source=model_info['source'],
                         model_revision=model_info['revision'], model_files=model_info['files'], local_files_only=True)
    fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    validation_seconds = clock() - validation_start
    emit('validation_completed')
    emit('model_load_started')
    load_start = clock()
    model = None
    load_error = None
    try:
        model = (backend_factory or _backend)(model_info['path'], device=device, compute_type=compute_type,
                                             cpu_threads=cpu_threads, num_workers=1, local_files_only=True)
    except Exception as exc:
        load_error = f'{type(exc).__name__}: {exc}'
    load_seconds = clock() - load_start
    emit('model_load_completed', model_load_seconds=load_seconds, succeeded=load_error is None)
    rows = []
    inference_completed = False
    backend_failure = None
    for sample in selected:
        emit('clip_started', clip_id=sample['id'])
        row = dict(id=sample['id'], audio_sha256=sample['audio']['sha256'], language_tags=list(sample['language_tags']),
                   raw='', segments=[], language=None, audio_duration_seconds=durations[sample['id']],
                   inference_seconds=None, queue_seconds=0.0, model_load_seconds=load_seconds,
                   inference_warmth='warm' if inference_completed else 'cold',
                   raw_score=None, error=None, status='pending')
        row.update(_decode_provenance(None, versions))
        inference_start = None
        try:
            if load_error:
                row['status'] = 'model_load_failed'
                raise RuntimeError('model load failed: ' + load_error)
            if backend_failure is not None:
                row['status'] = 'not_attempted_backend_failed'
                raise RuntimeError('not attempted due to prior backend failure: ' + backend_failure)
            audio, duration = load_wav_samples(manifest.parent / sample['audio']['path'])
            row['audio_duration_seconds'] = duration
            # faster-whisper expects a float32 ndarray (list would be decoded as a file).
            import numpy as np
            audio = np.asarray(audio, dtype=np.float32)
            emit('inference_started', clip_id=sample['id'])
            inference_start = clock()
            segments, info = model.transcribe(audio, task='transcribe', language=None)
            row['language'] = _language(info)
            row.update(_decode_provenance(info, versions))
            for segment in segments:
                start, end = float(segment.start), float(segment.end)
                if not isinstance(segment.text, str) or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start <= end:
                    raise ValueError('invalid ASR segment')
                row['segments'].append(dict(text=segment.text, start=start, end=end))
                row['raw'] += segment.text
            inference_completed = True
            row['status'] = 'succeeded'
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            if inference_start is not None:
                # Native state may be unsafe after any decode/iterator failure.
                # Retain this row, but never call this owned backend again.
                backend_failure = sample['id']
                row['status'] = 'inference_failed'
            elif row['status'] == 'pending':
                row['status'] = 'pre_inference_failed'
        finally:
            if inference_start is not None:
                row['inference_seconds'] = clock() - inference_start
        if row['status'] != 'not_attempted_backend_failed':
            row['raw_score'] = score_transcription(sample['verbatim'], row['raw']).to_dict()
        rows.append(row)
        emit('clip_completed', clip_id=sample['id'], inference_seconds=row['inference_seconds'], succeeded=row['error'] is None)
    failed = sum(row['error'] is not None for row in rows)
    emit('run_completed', succeeded=failed == 0)
    return dict(schema_version=1, mode='live', stage='asr', split=split,
                counts=dict(selected=len(rows), succeeded=len(rows)-failed, failed=failed), rows=rows,
                metadata=dict(manifest_sha256=_hash_file(manifest), configuration=configuration, backend_versions=versions,
                              configuration_fingerprint=fingerprint, model_source=model_info['source'],
                              model_revision=model_info['revision'], model_files=model_info['files'],
                              model_load_seconds=load_seconds, model_load_error=load_error,
                              model_load_warmth='unknown_os_cache', validation_seconds=validation_seconds,
                              inference_seconds=sum(r['inference_seconds'] or 0 for r in rows),
                              cpu_utilization_percent=None, gpu_memory_bytes=None,
                              energy_joules=None, power_watts=None,
                              resource_measurement_status='unavailable_not_instrumented'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--split', required=True, choices=['development', 'heldout'])
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--source')
    parser.add_argument('--revision')
    parser.add_argument('--device', required=True, choices=['cpu', 'cuda'])
    parser.add_argument('--compute-type', required=True)
    parser.add_argument('--cpu-threads', required=True, type=int)
    parser.add_argument('--output', required=True)
    parser.add_argument('--progress', action='store_true', help='log phases, clip IDs, and timings to stderr without transcripts')
    args = parser.parse_args(argv)
    def progress(event):
        print(json.dumps(event, ensure_ascii=True, allow_nan=False), file=sys.stderr, flush=True)
    try:
        report = run_live(args.manifest, args.split, args.snapshot, device=args.device,
                          compute_type=args.compute_type, cpu_threads=args.cpu_threads,
                          source=args.source, revision=args.revision, progress=progress if args.progress else None)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    return 1 if report['counts']['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
