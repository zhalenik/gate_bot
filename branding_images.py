"""Strict, self-contained SVG shapes and canonical bitmap branding assets."""
import io
import math
import re
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps

from branch_logos import canonical_image, MAX_UPLOAD_BYTES


SVG_NS = 'http://www.w3.org/2000/svg'
MAX_SVG_BYTES = 512 * 1024
TAGS = {'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
        'defs', 'linearGradient', 'radialGradient', 'stop', 'clipPath'}
ATTRS = {'viewBox', 'width', 'height', 'preserveAspectRatio', 'id', 'transform', 'fill',
         'stroke', 'color', 'fill-opacity', 'stroke-opacity', 'opacity', 'stroke-width',
         'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit', 'stroke-dasharray',
         'stroke-dashoffset', 'fill-rule', 'clip-rule', 'vector-effect', 'clip-path',
         'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'd', 'points',
         'offset', 'stop-color', 'stop-opacity', 'gradientUnits', 'gradientTransform',
         'spreadMethod', 'fx', 'fy', 'fr', 'clipPathUnits'}
PAINT = {'fill', 'stroke', 'color', 'fill-opacity', 'stroke-opacity', 'opacity',
         'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-miterlimit',
         'stroke-dasharray', 'stroke-dashoffset', 'fill-rule', 'clip-rule',
         'vector-effect', 'clip-path', 'stop-color', 'stop-opacity'}


def _svg_error():
    return ValueError('SVG должен содержать только векторные фигуры: без скриптов, внешних файлов, шрифтов и фильтров. Преобразуйте текст в кривые или загрузите PNG.')


def sanitize_svg(data):
    if len(data) > MAX_SVG_BYTES:
        raise ValueError('Размер SVG — не более 512 КБ')
    try:
        source = data.decode('utf-8-sig')
        if '\x00' in source or re.search(r'<!\s*(?:DOCTYPE|ENTITY)|<\?(?!xml\s)', source, re.I):
            raise _svg_error()
        root = ET.fromstring(source)
    except (UnicodeError, ET.ParseError):
        raise _svg_error() from None
    if root.tag not in {'svg', '{'+SVG_NS+'}svg'}:
        raise _svg_error()
    count = 0
    ids, references = set(), set()

    def clean(node, depth=0):
        nonlocal count
        count += 1
        if count > 2000 or depth > 32:
            raise ValueError('Слишком сложный SVG. Упростите изображение или загрузите PNG.')
        tag = node.tag.removeprefix('{'+SVG_NS+'}')
        if tag not in TAGS or (node.text or '').strip():
            raise _svg_error()
        attrs = dict(node.attrib)
        style = attrs.pop('style', '')
        for declaration in style.split(';'):
            if not declaration.strip():
                continue
            key, separator, value = declaration.partition(':')
            if not separator or key.strip() not in PAINT:
                raise _svg_error()
            attrs[key.strip()] = value.strip()
        normalized = {}
        for key, value in attrs.items():
            if key in {'version', 'role', 'aria-label'}:
                continue
            if key not in ATTRS or len(value) > 200000:
                raise _svg_error()
            match = re.fullmatch(r'url\(#([A-Za-z_][\w.-]{0,63})\)', value)
            if match:
                if key not in {'fill', 'stroke', 'clip-path'}:
                    raise _svg_error()
                references.add(match[1])
            elif not re.fullmatch(r'[A-Za-z0-9#.,+\-_\s()%]*', value) or re.search(r'url|javascript|expression|data:', value, re.I):
                raise _svg_error()
            if key == 'id':
                if not re.fullmatch(r'[A-Za-z_][\w.-]{0,63}', value) or value in ids:
                    raise _svg_error()
                ids.add(value)
            normalized[key] = value
        result = ET.Element('{'+SVG_NS+'}'+tag, normalized)
        for child in node:
            child_tag = child.tag.removeprefix('{'+SVG_NS+'}')
            if child_tag in {'title', 'desc', 'metadata'}:
                continue
            if (child.tail or '').strip():
                raise _svg_error()
            result.append(clean(child, depth+1))
        return result

    sanitized = clean(root)
    if references - ids:
        raise ValueError('SVG ссылается на отсутствующий градиент или область обрезки')
    viewbox = sanitized.get('viewBox')
    try:
        if viewbox:
            bounds = [float(value) for value in re.split(r'[\s,]+', viewbox.strip())]
            if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
                raise ValueError
            width, height = bounds[2:]
        else:
            width = float(sanitized.get('width', '').removesuffix('px'))
            height = float(sanitized.get('height', '').removesuffix('px'))
            sanitized.set('viewBox', f'0 0 {width:g} {height:g}')
        if not 0 < width <= 100000 or not 0 < height <= 100000 or not .02 <= width/height <= 50:
            raise ValueError
    except (ValueError, ZeroDivisionError):
        raise ValueError('У SVG должны быть корректные размеры или viewBox') from None
    sanitized.set('width', f'{width:g}')
    sanitized.set('height', f'{height:g}')
    ET.register_namespace('', SVG_NS)
    return ET.tostring(sanitized, encoding='utf-8'), width/height


def rasterize_svg(svg, aspect):
    import resvg_py
    width = 1024 if aspect >= 1 else max(1, round(1024*aspect))
    height = 1024 if aspect <= 1 else max(1, round(1024/aspect))
    try:
        return resvg_py.svg_to_bytes(svg_string=svg.decode('utf-8'), width=width,
                                    height=height, skip_system_fonts=True)
    except ValueError:
        raise ValueError('Не удалось отрисовать SVG. Упростите изображение или загрузите PNG.') from None


def canonical_brand(data):
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('Выберите изображение размером до 5 МБ')
    if data.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'<'):
        svg, aspect = sanitize_svg(data)
        pixels = rasterize_svg(svg, aspect)
        return svg, 'svg', pixels
    pixels = canonical_image(data)
    return pixels, 'png', pixels


def icon_png(pixels, size):
    """Opaque square includes padding, suitable for iOS and Android icons."""
    with Image.open(io.BytesIO(pixels)) as image:
        image = image.convert('RGBA')
        fitted = ImageOps.contain(image, (round(size*.8), round(size*.8)), Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (size, size), '#FFFFFF')
        canvas.alpha_composite(fitted, ((size-fitted.width)//2, (size-fitted.height)//2))
        output = io.BytesIO()
        canvas.convert('RGB').save(output, format='PNG', optimize=True)
        return output.getvalue()
