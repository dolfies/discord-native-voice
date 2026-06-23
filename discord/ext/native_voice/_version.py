from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as metadata_version
from pathlib import Path


def _read_cargo_version(cargo_toml: Path) -> str:
    in_package = False
    for line in cargo_toml.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line == '[package]':
            in_package = True
            continue
        if line.startswith('['):
            in_package = False
            continue
        if in_package and line.startswith('version'):
            return line.split('=', 1)[1].strip().strip('"')

    raise RuntimeError('Could not determine discord-native-voice version from Cargo.toml')


def _find_cargo_toml() -> Path | None:
    for path in Path(__file__).resolve().parents:
        cargo_toml = path / 'Cargo.toml'
        if cargo_toml.is_file():
            return cargo_toml
    return None


_cargo_toml = _find_cargo_toml()
if _cargo_toml is not None:
    __version__ = _read_cargo_version(_cargo_toml)
else:
    try:
        __version__ = metadata_version('discord-native-voice')
    except PackageNotFoundError:
        raise RuntimeError('Could not determine discord-native-voice version') from None
