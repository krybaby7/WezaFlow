"""Pure local artifact validation; native runtime setup is child-only."""
import hashlib
import json
import os
from pathlib import Path

REQUIRED_FILES = ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json', 'preprocessor_config.json')

def file_hash(path):
    digest = hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()

def validate_snapshot(path):
    root = Path(path)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('worker_snapshot_invalid')
    for name in REQUIRED_FILES:
        artifact = root / name
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise ValueError('worker_snapshot_incomplete')
    return root

def configure_runtime(manifest, expected_hash):
    if manifest is None:
        return []
    path = Path(manifest)
    if file_hash(path) != expected_hash:
        raise ValueError('worker_runtime_changed')
    entries = json.loads(path.read_text(encoding='utf-8'))
    files = []
    for package in entries:
        for entry in package['files']:
            relative = Path(entry['path'])
            target = (path.parent / relative).resolve()
            if relative.is_absolute() or not target.is_relative_to(path.parent.resolve()):
                raise ValueError('worker_runtime_path_invalid')
            if target.stat().st_size != entry['bytes'] or file_hash(target) != entry['sha256']:
                raise ValueError('worker_runtime_hash_invalid')
            files.append(target)
    dlls = [p for p in files if p.suffix.lower() == '.dll']
    if os.name != 'nt' or not dlls:
        raise ValueError('worker_runtime_platform_invalid')
    handles = [os.add_dll_directory(str(directory)) for directory in sorted({p.parent for p in dlls})]
    os.environ['PATH'] = os.pathsep.join(str(p) for p in sorted({p.parent for p in dlls})) + os.pathsep + os.environ.get('PATH','')
    import ctypes
    for name in ('cublasLt64_12.dll','cublas64_12.dll','cudnn64_9.dll'):
        matches = [p for p in dlls if p.name.lower() == name.lower()]
        if len(matches) != 1:
            raise ValueError('worker_runtime_required_dll_missing')
        handles.append(ctypes.WinDLL(str(matches[0])))
    return handles
