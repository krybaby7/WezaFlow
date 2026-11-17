"""Assemble this PC's trial without executing the application or any model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

ORIGINAL = Path('C:/Users/adami/projects/Hobby/Tier 1/WEZZA')
SOURCE = Path(__file__).resolve().parents[1]
MARKER = 'wezza-personal-trial-v1'
MODEL_BYTES = 5629109120


def assemble(exe=None, *, output_dir=None):
    default_target = ORIGINAL / 'artifacts/WezzaFlow'
    if output_dir is None:
        target = default_target
        marker = target / '.package-owner'
        if target.resolve() != target.absolute() or (target / 'Wezza.exe').exists():
            raise FileExistsError(
                f'WezzaFlow is already installed at {target}. Use --output-dir to stage a separate package.'
            )
        if target.exists() and not (marker.is_file() and marker.read_text() == MARKER):
            raise RuntimeError(f'Refusing to overwrite unrecognized directory: {target}')
        target.mkdir(parents=True, exist_ok=True)
    else:
        supplied = Path(output_dir)
        if supplied.is_symlink():
            raise FileExistsError(f'Output directory already exists: {supplied}')
        target = supplied.resolve()
        live = default_target.resolve()
        if target == live or live in target.parents:
            raise ValueError(f'Output directory must be outside the deployed trial: {live}')
        target.mkdir(parents=True, exist_ok=False)
        marker = target / '.package-owner'
    marker.write_text(MARKER)
    project = target / 'project'
    project.mkdir(exist_ok=True)
    shutil.copytree(SOURCE / 'services', project / 'services', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.db', '*.db-*'))
    shutil.copy2(SOURCE / 'pyproject.toml', project / 'pyproject.toml')
    (project / 'db').mkdir(exist_ok=True)
    shutil.copy2(SOURCE / 'db/schema.sql', project / 'db/schema.sql')
    config = project / 'config'
    config.mkdir(exist_ok=True)
    for name in ('app_profiles.yaml', 'snippets.yaml'):
        if not (config / name).exists():
            shutil.copy2(SOURCE / 'config' / name, config / name)
    assets = target / 'assets'
    assets.mkdir(exist_ok=True)
    model_dir = ORIGINAL / 'artifacts/m4-capacity-9b-2026-10-29'
    runtime = ORIGINAL / 'artifacts/m4-local-editor-runtime'
    records = json.loads((runtime / 'parent-verification.json').read_text())['files']

    def link(source, relative, size, digest):
        destination = assets / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if not os.path.samefile(source, destination):
                raise RuntimeError(f'Refusing to replace asset: {destination}')
        else:
            os.link(source, destination)
        if destination.stat().st_size != size:
            raise RuntimeError(f'Asset size mismatch: {destination}')
        return dict(path=relative, bytes=size, sha256=digest)

    model = link(model_dir / 'qwen3.5-9b-q4_k_m.gguf', 'qwen3.5-9b-q4_k_m.gguf',
                 MODEL_BYTES, '02d45dc1cf451ba2475ac33b301c2dd8f985abe4c182ce04a1f2f5bf0260278d')
    template_file = model_dir / 'chat_template.no-thinking.jinja'
    template = link(template_file, template_file.name, template_file.stat().st_size,
                    hashlib.sha256(template_file.read_bytes()).hexdigest())
    pins = [link(runtime / 'bin' / r['path'], 'bin/' + r['path'].replace('\\', '/'),
                 r['bytes'], r['sha256']) for r in records]
    server = next(r for r in pins if r['path'] == 'bin/llama-server.exe')
    manifest = dict(schema_version=1, source='Existing local Qwen3.5-9B trial assets; unqualified editor',
                    revision='existing-m4-capacity-9b-2026-10-29', model=model, template=template,
                    server=server, runtime_files=[r for r in pins if r is not server])
    manifest_path = assets / 'local-editor-manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    settings = f'''local_asr:
  snapshot_path: C:/Users/adami/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf
  runtime_manifest_path: {ORIGINAL.as_posix()}/artifacts/m3-cuda-runtime/verified-files.json
  device: cuda
  compute_type: int8_float16
  cpu_threads: 2
  startup_timeout_seconds: 90
  decode_timeout_seconds: 60
  idle_timeout_seconds: 60
  reap_timeout_seconds: 5
local_editor:
  enabled: true
  manifest_path: {json.dumps(manifest_path.as_posix(), ensure_ascii=False)}
  startup_timeout: 120
  request_timeout: 15
  idle_timeout: 60
  reap_timeout: 5
  threads: 2
  context_tokens: 4096
  output_tokens: 512
hotkeys:
  dictation:
    - Ctrl+Shift+Alt+F11
  command_mode:
    - Ctrl+Shift+Alt+F12
history:
  enabled: false
runtime:
  profile: local-multilingual
  language: auto
  quality_fallback: false
  system_audio_ducking: false
  use_ollama: false
  experimental_automatic_paste: false
'''
    if not (config / 'settings.yaml').exists():
        (config / 'settings.yaml').write_text(settings, encoding='utf-8')
    for name in ('Launch Wezza.vbs', 'launch.ps1', 'READ ME FIRST.md'):
        shutil.copy2(SOURCE / 'scripts/personal_trial' / name, target / name)
    (target / 'logs').mkdir(exist_ok=True)
    if exe:
        executable = Path(exe).resolve(strict=True)
        shutil.copy2(executable, target / 'Wezza.exe')
    print(target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', help='Final built desktop executable to copy as Wezza.exe')
    parser.add_argument('--output-dir', type=Path, help='Fresh directory for a separate trial package')
    args = parser.parse_args()
    assemble(args.exe, output_dir=args.output_dir)
