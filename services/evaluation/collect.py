"""Explicit, local-only collection of manually labelled personal dictation."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import sys
from collections.abc import Callable
from typing import Any
from .manifest import load_manifest, read_json
TEMPLATE = Path(__file__).resolve().parents[2] / 'tests/fixtures/dictation/collection-prompts.json'

def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.collection-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def initialize(output_dir: str | Path) -> Path:
    root = Path(output_dir).resolve()
    path = root / 'collection.json'
    if path.exists():
        raise ValueError('collection already exists; use a new output directory')
    _write(path, read_json(TEMPLATE))
    return path

def _load(path, sid=None):
    path = Path(path).resolve()
    data = read_json(path)
    if not isinstance(data, dict):
        raise ValueError('collection must be a JSON object')
    if data.get('schema_version') != 1 or not isinstance(data.get('samples'), list):
        raise ValueError('invalid collection schema or samples list')
    seen = set()
    for sample in data['samples']:
        if not isinstance(sample, dict):
            raise ValueError('collection sample must be an object')
        sample_id = sample.get('id')
        if not isinstance(sample_id, str) or not re.fullmatch(
            '[A-Za-z0-9][A-Za-z0-9._-]*', sample_id
        ):
            raise ValueError('invalid sample id')
        if sample_id in seen:
            raise ValueError('duplicate sample id: ' + sample_id)
        seen.add(sample_id)
        prompt = sample.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('sample prompt must be nonempty text')
    if sid is None:
        return (path, data)
    if not isinstance(sid, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', sid):
        raise ValueError('invalid sample id')
    matches = [s for s in data['samples'] if s.get('id') == sid]
    if len(matches) != 1:
        raise ValueError('unknown or duplicate sample id')
    return (path, data, matches[0])

def _audio(path, relative):
    rel = Path(relative)
    result = (path.parent / rel).resolve()
    if rel.is_absolute() or '..' in rel.parts or (not result.is_relative_to(path.parent)):
        raise ValueError('audio must stay inside collection directory')
    return result

def record(
    collection: str | Path,
    sid: str,
    seconds: float,
    *,
    input_fn: Callable[[str], str] = input,
    recorder: Callable[..., Any] | None = None,
) -> bool:
    """Record exactly one selected prompt after explicit terminal confirmation."""
    path, data, sample = _load(collection, sid)
    if not math.isfinite(seconds) or not 0 < seconds <= 120:
        raise ValueError('seconds must be between 0 and 120')
    target = _audio(path, f'audio/{sid}.wav')
    if target.exists() or sample.get('audio'):
        raise ValueError('recording exists; create a new sample/take instead')
    if input_fn(f"Prompt only: {sample['prompt']}\nRecord {sid} for {seconds:g}s now? Type yes: ").strip().lower() != 'yes':
        return False
    if recorder is None:
        from services.runtime.audio_smoke import record_wav
        recorder = record_wav
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('xb'):
        pass
    publication_started = False
    try:
        recorder(target, seconds=seconds)
        sample['audio'] = {
            'path': target.relative_to(path.parent).as_posix(),
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        }
        sample.pop('verbatim', None)
        sample.pop('acceptable_final_texts', None)
        publication_started = True
        _write(path, data)
    except BaseException:
        remove_target = not publication_started
        if publication_started:
            try:
                # Atomic replacement may have completed before _write raised.
                # Only delete when authoritative metadata proves noncommit;
                # an unreadable/uncertain state must retain the recording.
                persisted = _load(path, sid)[2]
                remove_target = 'audio' not in persisted
            except BaseException:
                remove_target = False
        if remove_target:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass  # Preserve the original capture/publication failure.
        raise
    return True

def annotate(
    collection: str | Path,
    sid: str,
    annotation: dict[str, Any],
    *,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Validate and save references only after the user confirms checking them."""
    path, data, sample = _load(collection, sid)
    if not sample.get('audio'):
        raise ValueError('record this sample before annotating')
    if not isinstance(annotation, dict):
        raise ValueError('annotation must be a JSON object')
    verbatim, finals = (annotation.get('verbatim'), annotation.get('acceptable_final_texts'))
    if (
        not isinstance(verbatim, str)
        or not verbatim.strip()
        or not isinstance(finals, list)
        or not finals
        or any(not isinstance(text, str) or not text.strip() for text in finals)
    ):
        raise ValueError('explicit nonempty verbatim and acceptable_final_texts required')
    allowed = {'verbatim', 'acceptable_final_texts', 'language_tags', 'metadata', 'protected_literals', 'spans'}
    if set(annotation) - allowed:
        raise ValueError('unknown annotation fields')
    candidate = {**sample, **annotation}
    _validate(path, [candidate])
    print(json.dumps(annotation, ensure_ascii=False, indent=2))
    if input_fn('I checked these references against my recording. Type yes to save: ').strip().lower() != 'yes':
        return False
    sample.update(annotation)
    sample['annotation_confirmed'] = True
    _write(path, data)
    return True

def _manifest_sample(sample):
    keys = ('id', 'audio', 'verbatim', 'acceptable_final_texts', 'language_tags', 'metadata', 'protected_literals', 'spans')
    return {**{k: sample[k] for k in keys if k in sample}, 'split': 'development'}

def _validate(path, samples):
    fd, name = tempfile.mkstemp(prefix='.validate-', suffix='.json', dir=path.parent)
    os.close(fd)
    try:
        _write(name, {'schema_version': 1, 'samples': [_manifest_sample(s) for s in samples]})
        return load_manifest(name)
    finally:
        Path(name).unlink(missing_ok=True)

def export(
    collection: str | Path,
    output: str | Path,
    *,
    ids: list[str] | None = None,
) -> Path:
    """Export explicitly confirmed development samples with verified audio."""
    path, data = _load(collection)
    output = Path(output).resolve()
    if output.parent != path.parent or output == path:
        raise ValueError('output must be a new file directly inside collection directory')
    if output.exists():
        raise ValueError('output already exists')
    samples = data['samples']
    if ids is not None:
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('select unique sample IDs')
        samples = [_load(path, sid)[2] for sid in ids]
    if not samples or any((not s.get('annotation_confirmed') for s in samples)):
        raise ValueError('incomplete annotations; use --id for explicitly selected ready samples')
    validated = _validate(path, samples)
    _write(output, validated)
    load_manifest(output)
    return output

def _configure_console() -> None:
    """Use UTF-8 for CLI pipes and terminals, tolerating replaced test streams."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            reconfigure(encoding='utf-8', errors='strict')

def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--output-dir', type=Path, default=Path('artifacts/dictation-corpus'))
    for name in ('record', 'annotate', 'export', 'status'):
        command = sub.add_parser(name)
        command.add_argument('--collection', type=Path, required=True)
        if name in ('record', 'annotate'):
            command.add_argument('--id', required=True)
        if name == 'record':
            command.add_argument('--seconds', type=float, default=8)
        if name == 'annotate':
            command.add_argument('--annotation', type=Path, required=True, help='JSON with manually checked verbatim and acceptable_final_texts')
        if name == 'export':
            command.add_argument('--output', type=Path, required=True)
            command.add_argument('--id', action='append', dest='ids')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            print(initialize(args.output_dir))
        elif args.command == 'record':
            record(args.collection, args.id, args.seconds)
        elif args.command == 'annotate':
            annotate(args.collection, args.id, read_json(args.annotation))
        elif args.command == 'export':
            print(export(args.collection, args.output, ids=args.ids))
        else:
            for s in _load(args.collection)[1]['samples']:
                if s.get('annotation_confirmed'):
                    state = 'annotated'
                elif s.get('audio'):
                    state = 'recorded'
                else:
                    state = 'draft'
                print(f"{s['id']}\t{state}\t{s['prompt']}")
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f'error: {exc}\n')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
