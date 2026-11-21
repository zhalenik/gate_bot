"""Strict, self-contained SVG shapes and canonical bitmap branding assets."""
import io
import math
import re
import xml.etree.ElementTree as ET

from PIL import Image

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


FRAME_DEFAULTS = {'fit':'cover', 'padding':0, 'background':'#FFFFFF', 'zoom':100, 'x':50, 'y':50}
LOGO_FRAME = {**FRAME_DEFAULTS, 'fit':'contain', 'background':'transparent'}


def frame_settings(value):
    if not isinstance(value,dict) or not {'fit','padding','background'} <= value.keys() <= FRAME_DEFAULTS.keys():
        raise ValueError('Проверьте параметры кадрирования')
    return validate_icon_frame(**value)


def submitted_frame(form):
    if 'icon_fit' not in form:
        return None
    try:
        numbers={k:int(form.get('icon_'+k, FRAME_DEFAULTS[k])) for k in ('padding','zoom','x','y')}
    except (TypeError,ValueError):
        raise ValueError('Проверьте масштаб, поля и положение изображения') from None
    return validate_icon_frame(form['icon_fit'],background='transparent' if 'icon_transparent' in form else form.get('icon_background'),**numbers)


def validate_icon_frame(fit, padding, background, zoom=100, x=50, y=50):
    if fit not in ('cover','contain') or type(padding) is not int or not 0 <= padding <= 30:
        raise ValueError('Выберите кадрирование и поля иконки от 0 до 30 %')
    if background != 'transparent' and (not isinstance(background,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', background)):
        raise ValueError('Выберите цвет фона иконки')
    if any(type(value) is not int or not low <= value <= high for value,low,high in ((zoom,100,400),(x,0,100),(y,0,100))):
        raise ValueError('Масштаб изображения должен быть от 100 до 400 %, положение — от 0 до 100 %')
    color = background if background == 'transparent' else background.upper()
    return {'fit':fit,'padding':padding,'background':color,'zoom':zoom,'x':x,'y':y}


def icon_png(pixels, size, *, fit='cover', padding=0, background='#FFFFFF', zoom=100, x=50, y=50):
    """Render only the visible source region, even for very wide zoomed uploads."""
    validate_icon_frame(fit,padding,background,zoom,x,y)
    with Image.open(io.BytesIO(pixels)) as image:
        image = image.convert('RGBA')
        width,height=image.size if size is None else (size,size)
        inner_w=max(1,round(width*(1-2*padding/100)))
        inner_h=max(1,round(height*(1-2*padding/100)))
        fit_scale = max if fit=='cover' else min
        scale=fit_scale(inner_w/image.width,inner_h/image.height)*zoom/100
        offset_x=(image.width-inner_w/scale)*x/100
        offset_y=(image.height-inner_h/scale)*y/100
        cropped=image.transform((inner_w,inner_h),Image.Transform.AFFINE,(1/scale,0,offset_x,0,1/scale,offset_y),
                                Image.Resampling.BICUBIC,fillcolor=(0,0,0,0))
        canvas=Image.new('RGBA',(width,height),(0,0,0,0) if background=='transparent' else background)
        canvas.alpha_composite(cropped,((width-inner_w)//2,(height-inner_h)//2))
        output = io.BytesIO()
        if background!='transparent':
            canvas=canvas.convert('RGB')
        canvas.save(output, format='PNG', optimize=True)
        return output.getvalue()
