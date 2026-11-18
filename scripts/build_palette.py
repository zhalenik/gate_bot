"""Inventory web CSS colors and give every declaration an independent override.

Run from the repository root: python scripts/build_palette.py [--check]
The fallback stays byte-for-byte equal to the original color expression. The
HEX sample is for the editor only and must never replace untouched fallbacks.
This deliberately does not parse email templates, SVG images, or CSS selectors
as color values. It performs no network, application-state or hardware work.
"""
from __future__ import annotations

import argparse
import colorsys
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

from PIL import ImageColor


ROOT = Path(__file__).resolve().parents[1]
PREFIX = '--gb-color-'
COLOR_NAMES = set(ImageColor.colormap) | {'transparent'}
PROPERTIES = {
    'color': 'Текст', 'background': 'Фон', 'background-color': 'Фон',
    'border': 'Рамка', 'border-color': 'Рамка', 'border-top': 'Верхняя рамка',
    'border-bottom': 'Нижняя рамка', 'border-left': 'Левая рамка',
    'border-right': 'Правая рамка', 'outline': 'Контур фокуса',
    'outline-color': 'Контур фокуса', 'box-shadow': 'Тень', 'text-shadow': 'Тень текста',
    'fill': 'Заливка SVG', 'stroke': 'Линия SVG', 'accent-color': 'Акцент элемента',
    '-webkit-tap-highlight-color': 'Подсветка нажатия',
}
NAMED = {
    '--lavender': 'Лавандовый — фирменные блоки', '--accent': 'Основные кнопки и акценты',
    '--accent-hover': 'Основные кнопки при наведении', '--ink': 'Основной текст',
    '--muted': 'Второстепенный текст', '--secondary': 'Второстепенные кнопки и блоки',
    '--line': 'Разделители и рамки', '--milk': 'Молочный фон',
    '--surface': 'Поверхность карточек', '--background': 'Фон страницы',
    '--sidebar': 'Боковые панели', '--focus': 'Фокус клавиатуры',
    '--soft-lavender': 'Светлый лавандовый фон', '--blue': 'Ссылки и общий акцент',
    '--green': 'Успех и доступность', '--red': 'Ошибки и запрет',
}
FILE_GROUPS = {
    'ui.css': 'Общие элементы', 'admin.css': 'Кабинет и таблицы',
    'access.css': 'Страница пропуска', 'gate-qr.css': 'Въезд по QR-коду',
    'help.css': 'Помощь', 'integrations.css': 'Интеграции',
    'support.css': 'Поддержка', 'telephony.css': 'Звонки и сообщения',
    'theme.css': 'Тема и оформление', 'palette.css': 'Редактор цветов',
    'style.css': 'Старые стили (не подключены)',
}
IDENTIFIER = re.compile(r'[A-Za-z_-][A-Za-z0-9_-]*')
HEX = re.compile(r'#[0-9a-fA-F]+\b')
REFERENCE = re.compile(r'var\(\s*(--[A-Za-z0-9_-]+)')


@dataclass(frozen=True)
class Declaration:
    file: str
    selector: str
    contexts: tuple[str, ...]
    property: str
    start: int
    end: int
    line: int
    value: str
    ordinal: int


def masked(text: str) -> str:
    """Preserve offsets/newlines while hiding strings and comments from syntax."""
    chars = list(text)
    index = 0
    while index < len(text):
        if text.startswith('/*', index):
            end = text.find('*/', index + 2)
            if end < 0:
                raise ValueError('Unclosed CSS comment')
            end += 2
        elif text[index] in {'"', "'"}:
            quote, end = text[index], index + 1
            while end < len(text):
                if text[end] == '\\':
                    end += 2
                elif text[end] == quote:
                    end += 1
                    break
                else:
                    end += 1
            else:
                raise ValueError('Unclosed CSS string')
        else:
            index += 1
            continue
        for position in range(index, min(end, len(text))):
            if chars[position] not in '\r\n':
                chars[position] = ' '
        index = end
    return ''.join(chars)


def matching(text: str, start: int, left='(', right=')') -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == left:
            depth += 1
        elif text[index] == right:
            depth -= 1
            if depth == 0:
                return index
    raise ValueError('Unbalanced CSS delimiters')


def unwrap_generated(text: str) -> str:
    """An idempotent rebuild reads original fallbacks from our own wrappers."""
    while True:
        syntax = masked(text)
        match = re.search(r'var\(\s*' + re.escape(PREFIX) + r'[a-z0-9-]+\s*,', syntax)
        if not match:
            return text
        end = matching(syntax, match.start() + 3)
        text = text[:match.start()] + text[match.end():end].strip() + text[end + 1:]


def declarations(file: str, text: str) -> list[Declaration]:
    syntax = masked(text)
    result = []
    counts = defaultdict(int)

    def block(start, end, selector, contexts):
        position = segment = start
        depth = 0
        while position <= end:
            char = syntax[position] if position < end else ';'
            if char in '([':
                depth += 1
            elif char in ')]':
                depth -= 1
            if char == ';' and depth == 0:
                value_start = syntax.find(':', segment, position)
                if value_start >= 0:
                    prop = syntax[segment:value_start].strip()
                    if not re.fullmatch(r'--[\w-]+|-?[A-Za-z][\w-]*', prop):
                        raise ValueError('Unsupported CSS declaration in ' + file)
                    begin, finish = value_start + 1, position
                    while begin < finish and text[begin].isspace():
                        begin += 1
                    while finish > begin and text[finish - 1].isspace():
                        finish -= 1
                    key = (selector, contexts, prop)
                    counts[key] += 1
                    result.append(Declaration(file, selector, contexts, prop, begin, finish,
                                              text.count('\n', 0, begin) + 1,
                                              text[begin:finish], counts[key]))
                segment = position + 1
            position += 1

    def rules(start, end, contexts):
        position = header = start
        depth = 0
        while position < end:
            char = syntax[position]
            if char in '([':
                depth += 1
            elif char in ')]':
                depth -= 1
            elif char == ';' and depth == 0:
                header = position + 1  # e.g. @charset, @import
            elif char == '{' and depth == 0:
                close = matching(syntax, position, '{', '}')
                title = re.sub(r'/\*.*?\*/', '', text[header:position], flags=re.S).strip()
                if title.startswith(('@media', '@supports', '@layer', '@container', '@keyframes', '@-webkit-keyframes')):
                    rules(position + 1, close, contexts + (title,))
                else:
                    block(position + 1, close, title, contexts)
                position = close
                header = close + 1
            position += 1

    rules(0, len(text), ())
    return result


def color_spans(value: str):
    syntax = masked(value)
    index = 0
    while index < len(value):
        if syntax[index] == '#':
            token = HEX.match(syntax, index)
            if token and len(token.group()) in {4, 5, 7, 9}:
                yield index, token.end(), value[index:token.end()]
                index = token.end()
                continue
        token = IDENTIFIER.match(syntax, index)
        if token:
            name = token.group().lower()
            after = token.end()
            while after < len(syntax) and syntax[after].isspace():
                after += 1
            if after < len(syntax) and syntax[after] == '(' and name in {'rgb', 'rgba', 'hsl', 'hsla', 'url'}:
                end = matching(syntax, after) + 1
                if name != 'url':
                    yield index, end, value[index:end]
                index = end
                continue
            if name in COLOR_NAMES:
                yield index, token.end(), value[index:token.end()]
            index = token.end()
        else:
            index += 1


def resolve_variables(value: str, definitions: dict[str, str], depth=0) -> str:
    if depth > 20:
        raise ValueError('Cyclic CSS color reference')
    syntax = masked(value)
    match = REFERENCE.search(syntax)
    if not match:
        return value
    end = matching(syntax, match.start() + 3)
    rest = value[match.end():end].strip()
    fallback = rest[1:].strip() if rest.startswith(',') else None
    target = definitions.get(match.group(1), fallback)
    if target is None:
        raise ValueError('CSS color variable needs a default: ' + match.group(1))
    target = resolve_variables(target, definitions, depth + 1)
    return resolve_variables(value[:match.start()] + target + value[end + 1:], definitions, depth + 1)


def canonical_color(value: str, definitions=None) -> str:
    value = resolve_variables(value, definitions or {}).strip()
    if value.lower() == 'transparent':
        return '#00000000'
    if value.startswith('#'):
        digits = value[1:]
        if len(digits) in {3, 4}:
            digits = ''.join(char * 2 for char in digits)
        if len(digits) not in {6, 8} or not re.fullmatch('[0-9A-Fa-f]+', digits):
            raise ValueError('Unsupported CSS hex color')
        return '#' + digits.upper()
    function = re.fullmatch(r'(rgba?|hsla?)\((.*)\)', value, re.I | re.S)
    if not function:
        rgb = ImageColor.getrgb(value)
        return '#' + ''.join(f'{channel:02X}' for channel in rgb)
    args = re.split(r'[\s,/]+', function.group(2).strip())
    if len(args) not in {3, 4}:
        raise ValueError('Unsupported CSS color expression')
    if function.group(1).lower().startswith('rgb'):
        channels = [float(arg[:-1]) * 2.55 if arg.endswith('%') else float(arg) for arg in args[:3]]
    else:
        hue = args[0]
        multiplier = 360 if hue.endswith('turn') else 180 / math.pi if hue.endswith('rad') and not hue.endswith('grad') else .9 if hue.endswith('grad') else 1
        hue = float(re.sub(r'(deg|grad|rad|turn)$', '', hue)) * multiplier
        saturation, lightness = (float(arg[:-1]) / 100 for arg in args[1:3])
        channels = [v * 255 for v in colorsys.hls_to_rgb((hue % 360) / 360, lightness, saturation)]
    result = '#' + ''.join(f'{max(0, min(255, round(channel))):02X}' for channel in channels)
    if len(args) == 4:
        alpha = float(args[3][:-1]) / 100 if args[3].endswith('%') else float(args[3])
        result += f'{max(0, min(255, round(alpha * 255))):02X}'
    return result


def location(declaration: Declaration):
    return {'file': declaration.file, 'line': declaration.line,
            'selector': declaration.selector, 'property': declaration.property,
            'context': ' › '.join(declaration.contexts)}


def build_sources(sources: dict[str, str]):
    originals = {path: unwrap_generated(text) for path, text in sources.items()}
    parsed = {path: declarations(path, text) for path, text in originals.items()}
    # The base template loads ui/admin, then page CSS, then theme. Legacy style
    # is inventoried but does not override the current application's root vars.
    order = sorted(parsed, key=lambda path: (0 if path.endswith('/ui.css') else 2 if path.endswith('/theme.css') else 1, path))
    definitions, effective = {}, {}
    for path in order:
        if path.endswith('/style.css'):
            continue
        for item in parsed[path]:
            if item.property in NAMED and item.selector == ':root' and not item.contexts:
                definitions[item.property] = item.value
                effective[item.property] = item
    entries, replacements, named_entries = [], defaultdict(list), defaultdict(list)
    for path in sorted(parsed):
        for declaration in parsed[path]:
            source = declaration.value
            alias = declaration.property in NAMED and re.fullmatch(r'var\(\s*--[\w-]+\s*\)', source)
            spans = [(0, len(source), source)] if alias else list(color_spans(source))
            for number, (start, end, original) in enumerate(spans):
                seed = [path, declaration.contexts, ' '.join(declaration.selector.split()), declaration.property,
                        declaration.ordinal, number]
                digest = hashlib.sha256(json.dumps(seed, ensure_ascii=False).encode()).hexdigest()[:12]
                prop = declaration.property.strip('-').lower()
                identifier = Path(path).stem + '-' + prop + '-' + digest
                css_var = PREFIX + identifier
                is_named = declaration.property in NAMED and declaration.selector == ':root'
                active = not path.endswith('/style.css') and (not is_named or effective.get(declaration.property) == declaration)
                group = 'Основная палитра' if is_named and active else FILE_GROUPS.get(Path(path).name, Path(path).stem)
                if is_named and not active:
                    group = 'Базовые цвета (перекрыты темой)'
                label = NAMED[declaration.property] if is_named else PROPERTIES.get(declaration.property, declaration.property) + ' · ' + declaration.selector
                if len(spans) > 1:
                    label += ' · цвет ' + str(number + 1)
                dynamic = 'var(' in original and not alias
                if dynamic:
                    label += ' · динамический цвет, показан пример'
                status = declaration.property in {'--red', '--green'} or bool(re.search(r'success|warning|danger|error|denied|permanent|temporary|signal|connection|status|badge', declaration.selector, re.I))
                entry = {'id': identifier, 'css_var': css_var, 'label': label, 'group': group,
                         'source_value': original, 'default': canonical_color(original, definitions),
                         'editable': True, 'status_color': status, 'active': active,
                         'legacy': path.endswith('/style.css'), 'dynamic': bool(dynamic),
                         'locations': [location(declaration)]}
                entries.append(entry)
                if is_named:
                    named_entries[declaration.property].append((entry, declaration))
                if not path.endswith('/palette.css'):
                    replacements[path].append((declaration.start + start, declaration.start + end,
                                               f'var({css_var}, {original})'))
                else:
                    raise ValueError('palette.css must use existing color variables, not new literals')

    references = defaultdict(list)
    aliases = defaultdict(set)
    for path in sorted(parsed):
        for item in parsed[path]:
            for reference in set(REFERENCE.findall(masked(item.value))):
                references[reference].append(location(item))
                if effective.get(item.property) == item:
                    aliases[reference].add(item.property)
    def all_references(prop, visited=None):
        visited = set(visited or ())
        if prop in visited:
            return []
        visited.add(prop)
        result = list(references[prop])
        for child in sorted(aliases[prop]):
            result.extend(all_references(child, visited))
        return result
    for prop, values in named_entries.items():
        for entry, definition in values:
            if effective.get(prop) == definition:
                seen = {json.dumps(entry['locations'][0], sort_keys=True)}
                for use in all_references(prop):
                    key = json.dumps(use, sort_keys=True)
                    if key not in seen:
                        entry['locations'].append(use)
                        seen.add(key)
    entries.sort(key=lambda row: (row['group'] != 'Основная палитра', row['group'], row['label'], row['id']))
    version = hashlib.sha256(json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    transformed = {}
    for path, text in originals.items():
        for start, end, replacement in sorted(replacements[path], reverse=True):
            text = text[:start] + replacement + text[end:]
        transformed[path] = text
    return {'catalog_version': version, 'entries': entries}, transformed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Report stale inventory or unwrapped colors without changing files')
    args = parser.parse_args()
    paths = sorted((ROOT / 'static' / 'css').glob('*.css'))
    sources = {path.relative_to(ROOT).as_posix(): path.read_text(encoding='utf-8') for path in paths}
    catalog, transformed = build_sources(sources)
    serialized = json.dumps(catalog, ensure_ascii=False, indent=2) + '\n'
    target = ROOT / 'resources' / 'palette_catalog.json'
    stale = [path for path, text in transformed.items() if text != sources[path]]
    if not target.exists() or target.read_text(encoding='utf-8') != serialized:
        stale.append('resources/palette_catalog.json')
    if args.check:
        if stale:
            print('Palette inventory is stale: ' + ', '.join(stale))
            return 1
    else:
        for path, text in transformed.items():
            if text != sources[path]:
                (ROOT / path).write_text(text, encoding='utf-8', newline='\n')
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f'Palette: {len(catalog["entries"])} entries, {len(paths)} stylesheets, inventory current')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
