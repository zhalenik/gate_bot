"""CSS migration boundaries: cascade, gradients, alpha and inherited tokens."""
from pathlib import Path

from scripts.build_palette import build_sources, canonical_color, unwrap_generated


def test_inventory_changes_only_declaration_colors_and_keeps_original_fallbacks():
    source = '''/* #aabbcc is a comment, not an editable color. */
:root{--accent:#abc;--blue:var(--accent);--surface:rgba(255,255,255,.45)}
[stroke="#007aff"]::before{content:"white #fff";background:linear-gradient(white,#fff8);border-color:transparent;mask:url("data:image/svg+xml,<svg fill='#fff'/>")}
@media(max-width:1px){.sample{box-shadow:0 0 1px rgba(0,0,0,.3);color:currentColor}}
.alias{color:var(--blue)}'''
    catalog, result = build_sources({'static/css/theme.css': source})
    converted = result['static/css/theme.css']
    assert '[stroke="#007aff"]' in converted and 'content:"white #fff"' in converted
    assert 'url("data:image/svg+xml,<svg fill=\'#fff\'/>")' in converted
    assert 'color:currentColor' in converted
    assert 'rgba(255,255,255,.45)' in converted
    assert unwrap_generated(converted) == source
    assert len(catalog['entries']) == 7
    entries = {row['locations'][0]['property']: row for row in catalog['entries']}
    assert entries['--surface']['source_value'] == 'rgba(255,255,255,.45)'
    assert entries['--blue']['source_value'] == 'var(--accent)'
    assert entries['--blue']['default'] == '#AABBCC'
    assert any(place['selector'] == '.alias' for place in entries['--accent']['locations'])
    assert entries['box-shadow']['locations'][0]['context'] == '@media(max-width:1px)'


def test_rebuild_is_idempotent_and_identical_hex_can_be_changed_independently():
    source = '.cta{color:#fff}.status{color:#fff}@media(min-width:1px){.cta{color:#fff}}'
    first, transformed = build_sources({'static/css/theme.css': source})
    second, repeated = build_sources(transformed)
    assert first == second and transformed == repeated
    assert len({row['css_var'] for row in first['entries']}) == 3
    assert len({row['default'] for row in first['entries']}) == 1
    assert len({row['id'] for row in first['entries']}) == 3


def test_dynamic_avatar_keeps_per_element_hue_and_keyframes_are_inventoried():
    source = '.avatar{background:hsl(var(--avatar-hue,215) 38% 90%)}@keyframes pulse{from{color:#fff}to{color:black}}'
    catalog, converted = build_sources({'static/css/telephony.css': source})
    assert 'hsl(var(--avatar-hue,215) 38% 90%)' in converted['static/css/telephony.css']
    dynamic = next(row for row in catalog['entries'] if row['dynamic'])
    assert dynamic['default'] == '#DCE4EF'
    assert any(place['context'] == '@keyframes pulse' for row in catalog['entries'] for place in row['locations'])


def test_sample_colors_are_valid_picker_values_but_preserve_full_alpha_fallback():
    assert canonical_color('#f008') == '#FF000088'
    assert canonical_color('transparent') == '#00000000'
    assert canonical_color('rgb(100% 0% 0% / 50%)') == '#FF000080'
    assert canonical_color('hsl(0.5turn 100% 50%)') == '#00FFFF'


def test_repository_inventory_is_current_complete_and_marks_unused_definitions():
    from palette_catalog import load_catalog
    root = Path(__file__).resolve().parents[1]
    sources = {path.relative_to(root).as_posix(): path.read_text(encoding='utf-8')
               for path in (root / 'static' / 'css').glob('*.css')}
    expected, transformed = build_sources(sources)
    assert transformed == sources, 'Run scripts/build_palette.py after changing CSS colors'
    assert load_catalog() == expected
    entries = expected['entries']
    assert len(entries) > 300 and len({entry['id'] for entry in entries}) == len(entries)
    primary = [row for row in entries if row['group'] == 'Основная палитра']
    assert len(primary) == 16
    assert all(row['active'] for row in primary)
    legacy = [row for row in entries if row['legacy']]
    assert legacy and all(not row['active'] for row in legacy)
    assert all(row['locations'] and row['source_value'] for row in entries)
    # Callers may add current values without mutating the cached catalog.
    altered = load_catalog(); altered['entries'][0]['label'] = 'changed only in caller'
    assert load_catalog() == expected


def test_alias_current_color_follows_saved_parent_until_explicitly_overridden():
    from palette_catalog import current_colors
    catalog, _ = build_sources({'static/css/theme.css': ':root{--accent:#123456;--blue:var(--accent);--background:var(--blue)}'})
    named = {row['locations'][0]['property']: row['id'] for row in catalog['entries']}
    colors = current_colors(catalog, {named['--accent']: '#AABBCC80'})
    assert colors[named['--blue']] == colors[named['--background']] == '#AABBCC80'
    colors = current_colors(catalog, {named['--accent']: '#AABBCC80', named['--blue']: '#445566'})
    assert colors[named['--blue']] == colors[named['--background']] == '#445566'
    assert all(row['source_value'].startswith(('var(', '#')) for row in catalog['entries'])
    # A bad future catalog cannot recurse indefinitely even though the build
    # command normally rejects circular aliases before writing a catalog.
    accent = next(row for row in catalog['entries'] if row['id'] == named['--accent'])
    accent['source_value'] = 'var(--background)'
    assert current_colors(catalog, {})[named['--accent']] == accent['default']
