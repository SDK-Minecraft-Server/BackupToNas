#!/usr/bin/env python3
"""Build the MCDR plugin archive used by the release workflow."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parent
METADATA_FILE = ROOT / 'mcdreforged.plugin.json'
REQUIRED_FILES = (
    Path('mcdreforged.plugin.json'),
    Path('LICENSE'),
    Path('requirements.txt'),
    Path('backup_to_nas/__init__.py'),
    Path('lang/en_us.json'),
    Path('lang/zh_cn.json'),
)
PACKAGE_PATHS = (
    Path('mcdreforged.plugin.json'),
    Path('LICENSE'),
    Path('requirements.txt'),
    Path('backup_to_nas'),
    Path('lang'),
)


def metadata_value(metadata: dict, field: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f'{field} must be a non-empty string')
    # Match the release workflow's previous ``tr -d [:space:]`` behavior.
    value = ''.join(value.split())
    if not value or '/' in value or '\\' in value:
        raise ValueError(f'invalid plugin {field}: {value!r}')
    return value


def package_files():
    for relative_path in PACKAGE_PATHS:
        path = ROOT / relative_path
        if path.is_file():
            yield path, relative_path.as_posix()
            continue
        if not path.is_dir():
            continue
        for child in sorted(path.rglob('*')):
            if not child.is_file():
                continue
            relative_child = child.relative_to(ROOT)
            if '__pycache__' in relative_child.parts or child.suffix == '.pyc':
                continue
            yield child, relative_child.as_posix()


def build_archive() -> Path:
    try:
        metadata = json.loads(METADATA_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'cannot read {METADATA_FILE.name}: {error}') from error
    if not isinstance(metadata, dict):
        raise ValueError(f'{METADATA_FILE.name} must contain a JSON object')

    plugin_name = metadata_value(metadata, 'name')
    plugin_version = metadata_value(metadata, 'version')

    missing = [str(path) for path in REQUIRED_FILES if not (ROOT / path).is_file()]
    if missing:
        raise FileNotFoundError('required package files are missing: ' + ', '.join(missing))

    output_dir = ROOT / 'dist'
    output_dir.mkdir(exist_ok=True)
    archive = output_dir / f'{plugin_name}-{plugin_version}.mcdr'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        for path, archive_path in package_files():
            package.write(path, archive_path)

    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError(f'archive verification failed: {archive}')
        members = set(package.namelist())
        for required_member in ('lang/en_us.json', 'lang/zh_cn.json'):
            if required_member not in members:
                raise ValueError(f'archive is missing {required_member}')
    return archive.relative_to(ROOT)


def main() -> int:
    try:
        print(build_archive())
    except (OSError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
