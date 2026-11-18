"""Read the generated public CSS inventory without application side effects."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re


CATALOG_PATH = Path(__file__).resolve().parent / 'resources' / 'palette_catalog.json'


@lru_cache(maxsize=2)
def _load(path, modified, size):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load_catalog():
    stat = CATALOG_PATH.stat()
    return deepcopy(_load(str(CATALOG_PATH), stat.st_mtime_ns, stat.st_size))


def current_colors(catalog, overrides):
    """Resolve visible direct aliases without turning inheritance into an edit.

    Dynamic component expressions still use their labelled sample: per-element
    values such as an avatar hue are intentionally not inferred server-side.
    """
    entries = {entry['id']: entry for entry in catalog['entries']}
    named = {}
    for entry in entries.values():
        if not entry.get('active', True) or not entry.get('locations'):
            continue
        definition = entry['locations'][0]
        if definition.get('selector') == ':root' and definition.get('property', '').startswith('--'):
            named[definition['property']] = entry['id']
    colors = {}

    def resolve(identifier, visited):
        entry = entries[identifier]
        if identifier in overrides:
            return overrides[identifier]
        if identifier in visited:
            return entry['default']
        match = re.fullmatch(r'var\(\s*(--[\w-]+)\s*\)', entry.get('source_value', ''))
        target = named.get(match.group(1)) if match else None
        if target:
            return resolve(target, visited | {identifier})
        return entry['default']

    for identifier in entries:
        colors[identifier] = resolve(identifier, set())
    return colors
