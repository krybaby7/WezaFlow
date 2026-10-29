"""Guided, local-only collection of six manually reviewed speech samples."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import wave

from . import collect

DEFAULT_IDS = ('en-01', 'en-06', 'fr-01', 'fr-06', 'ar-01', 'ar-06')
SCRIPT_POLICY = (
    'Verbatim reference: keep spoken corrections and repetitions; write Arabic words '
    'in Arabic script, even inside English/French speech, and English/French in Latin. '
    'For intended FINAL text, check each spoken sentence: Arabic-start uses Arabic script for Arabic spans '
    'and Latin for English/French. English/French-start uses Latin throughout, '
    'with numbered Arabizi for Egyptian Arabic (e.g. 3ayez, 7aga). '
    'Prompts are reading aids, never ground truth. Enter what you actually said.'
)
# A 120-second mono PCM16 recording plus a bounded allowance for WAV headers.
MAX_WAV_BYTES = 120 * 16000 * 2 + 65536


def _verified_audio(collection, sample):
    audio = sample.get('audio')
    if not isinstance(audio, dict) or not isinstance(audio.get('path'), str) or not audio['path']:
        raise ValueError('invalid recorded audio path')
    path = collect._audio(collection, audio['path'])
    digest = audio.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('invalid audio sha256')
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_WAV_BYTES:
        raise ValueError('missing or oversized recorded WAV')
    with path.open('rb') as stream:
        contents = stream.read(MAX_WAV_BYTES + 1)
    if len(contents) > MAX_WAV_BYTES or hashlib.sha256(contents).hexdigest() != digest:
        raise ValueError('audio checksum mismatch or oversized WAV')
    # Reuse the production PCM contract only after bounding the file read/allocation.
    from services.runtime.session import wav_bytes_to_float_samples
    try:
        samples = wav_bytes_to_float_samples(contents)
    except (wave.Error, EOFError, RuntimeError) as exc:
        raise ValueError('invalid recorded WAV: ' + str(exc)) from exc
    if not 0 < len(samples) <= 120 * 16000:
        raise ValueError('recorded WAV duration must be between 0 and 120 seconds')
    return path


def _play(path):
    try:
        import winsound
    except ImportError as exc:
        raise RuntimeError('Playback requires Windows; listen to the WAV externally and resume review') from exc
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)


def _review(collection, sid, input_fn, player):
    sample = collect._load(collection, sid)[2]
    audio = _verified_audio(collection, sample)
    print(f'Verified recording: {audio}')
    if input_fn('Play this recording now? Type yes (otherwise listen externally): ').strip().lower() == 'yes':
        player(audio)
    print(SCRIPT_POLICY)
    print('Metadata for confirmation: ' + json.dumps(sample.get('metadata'), ensure_ascii=False))
    print('For metadata corrections or multiple final variants, use the manual annotate command.')
    verbatim = input_fn('Verbatim actual speech, including corrections/repetitions (blank cancels review): ')
    if not verbatim.strip():
        print('Review cancelled; recording retained.')
        return
    final = input_fn('Intended final text (blank cancels review): ')
    if not final.strip():
        print('Review cancelled; recording retained.')
        return
    suggested = ','.join(sample.get('language_tags', []))
    tags_text = input_fn(f'Actual language tags EN,FR,AR,mixed, comma separated [{suggested}]: ').strip()
    tags = [tag.strip() for tag in tags_text.split(',')] if tags_text else sample.get('language_tags')
    # Recheck before annotation, including after optional external playback/review.
    _verified_audio(collection, sample)
    collect.annotate(collection, sid, {
        'verbatim': verbatim, 'acceptable_final_texts': [final], 'language_tags': tags,
    }, input_fn=input_fn)


def run(output_dir, *, ids=None, seconds=12, export_filename='manifest.json',
        input_fn=None, recorder=None, player=None):
    """Collect/review selected samples; injected devices keep tests fully offline."""
    input_fn = input if input_fn is None else input_fn
    player = _play if player is None else player
    selected = list(DEFAULT_IDS if ids is None else ids)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError('select unique nonempty sample IDs')
    if not math.isfinite(seconds) or not 0 < seconds <= 120:
        raise ValueError('seconds must be between 0 and 120')
    root = Path(output_dir).resolve()
    collection = root / 'collection.json'
    name = Path(export_filename)
    output = (root / name).resolve()
    if name.name != str(name) or name.suffix.lower() != '.json' or output.parent != root or output == collection:
        raise ValueError('export filename must be a new .json filename directly inside collection directory')
    if output.exists():
        raise ValueError('export already exists; choose a new export filename')
    data = collect._load(collection)[1] if collection.exists() else collect.read_json(collect.TEMPLATE)
    available = {sample['id'] for sample in data['samples']}
    if any(sid not in available for sid in selected):
        raise ValueError('unknown sample ID in selection')
    if not collection.exists():
        collect.initialize(root)
    print(f'Collection: {collection}\nSelected: {", ".join(selected)}')
    print('Retakes require a fresh directory. Saved audio is never overwritten.')
    for sid in selected:
        sample = collect._load(collection, sid)[2]
        if sample.get('annotation_confirmed'):
            _verified_audio(collection, sample)
            print(f'{sid}: confirmed; retained.')
            continue
        print(f"\n{sid} — prompt only: {sample['prompt']}")
        existing = bool(sample.get('audio'))
        action_name = 'review' if existing else 'record'
        while True:
            action = input_fn(f'Action [{action_name}/skip/quit]: ').strip().lower()
            if action in (action_name, 'skip', 'quit'):
                break
            print(f'Enter {action_name}, skip or quit.')
        if action == 'quit':
            print('Stopped; saved work retained. No manifest exported.')
            return None
        if action == 'skip':
            continue
        if not existing:
            kwargs = {'input_fn': input_fn}
            if recorder is not None:
                kwargs['recorder'] = recorder
            if not collect.record(collection, sid, seconds, **kwargs):
                continue
        _review(collection, sid, input_fn, player)
    samples = [collect._load(collection, sid)[2] for sid in selected]
    if any(not sample.get('annotation_confirmed') for sample in samples):
        print('Selected set incomplete; saved work retained. No manifest exported. Resume with the same command.')
        return None
    print('Confirmed selected set: ' + ', '.join(selected))
    if input_fn(f'Export this complete selected set to {output}? Type yes: ').strip().lower() == 'yes':
        result = collect.export(collection, output, ids=selected)
        print(f'Exported {result}. No model evaluation was run.')
        return result
    print('Export declined; saved work retained.')
    return None


def main(argv=None):
    collect._configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--id', action='append', dest='ids')
    parser.add_argument('--seconds', type=float, default=12)
    parser.add_argument('--export-filename', default='manifest.json')
    args = parser.parse_args(argv)
    try:
        run(args.output_dir, ids=args.ids, seconds=args.seconds, export_filename=args.export_filename)
    except (KeyboardInterrupt, EOFError):
        print('\nInterrupted; saved work is retained. Check collection status and the export output before resuming.')
        return 130
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f'error: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
