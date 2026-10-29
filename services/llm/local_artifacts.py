"""Strict, local-only artifact manifest. Never resolves or downloads artifacts."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('artifact_duplicate_key')
        result[key] = value
    return result


def verify_manifest(path):
    path = Path(path)
    if not path.is_absolute() or path.stat().st_size > 65536:
        raise ValueError('artifact_manifest_invalid')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('artifact_manifest_invalid')
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value,dict) or set(value) != {'schema_version', 'source', 'revision', 'model', 'template', 'server', 'runtime_files'} or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('artifact_schema_invalid')
    for key in ('source', 'revision'):
        if not isinstance(value[key], str) or not 0 < len(value[key]) <= 512:
            raise ValueError('artifact_metadata_invalid')
    if not isinstance(value['runtime_files'], list) or len(value['runtime_files']) > 128:
        raise ValueError('artifact_runtime_invalid')
    seen = set()
    root = path.parent.resolve()
    for record in [value['model'], value['template'], value['server'], *value['runtime_files']]:
        if not isinstance(record, dict) or set(record) != {'path', 'bytes', 'sha256'}:
            raise ValueError('artifact_record_invalid')
        name = record['path']
        if not isinstance(name, str) or not 0 < len(name) <= 512:
            raise ValueError('artifact_path_invalid')
        win = PureWindowsPath(name)
        if win.is_absolute() or win.drive or ':' in name or any(part in ('..', '.') for part in win.parts):
            raise ValueError('artifact_path_invalid')
        resolved = (root / name).resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file() or str(resolved).casefold() in seen:
            raise ValueError('artifact_path_invalid')
        seen.add(str(resolved).casefold())
        if type(record['bytes']) is not int or record['bytes'] <= 0 or resolved.stat().st_size != record['bytes']:
            raise ValueError('artifact_size_invalid')
        if not isinstance(record['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', record['sha256']):
            raise ValueError('artifact_hash_invalid')
        digest = hashlib.sha256()
        with resolved.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != record['sha256']:
            raise ValueError('artifact_hash_mismatch')
        record['absolute_path'] = str(resolved)
    value['manifest_sha256'] = hashlib.sha256(raw).hexdigest()
    return value
