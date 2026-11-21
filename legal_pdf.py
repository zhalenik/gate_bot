"""Printable editions of GateBot's bundled legal texts, with embedded Cyrillic fonts."""
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from xml.sax.saxutils import escape

from font_roboto import font_directory
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate


DIRECTORY = Path(__file__).parent / 'resources' / 'legal'
DOCUMENTS = ('admin-privacy', 'client-privacy', 'terms', 'offer')
INK = HexColor('#0C0F14')
MUTED = HexColor('#515362')
for weight in ('Regular', 'Bold'):
    pdfmetrics.registerFont(TTFont(f'GateBot-{weight}', str(Path(font_directory) / f'Roboto-{weight}.ttf')))


@lru_cache(maxsize=4)
def bundled_text(kind):
    if kind not in DOCUMENTS:
        raise ValueError('Неизвестный документ')
    return (DIRECTORY / (kind + '.txt')).read_text(encoding='utf-8')


@lru_cache(maxsize=4)
def bundled_text_hashes(kind):
    source = bundled_text(kind)
    # Existing installations may have imported the same text from a Windows checkout.
    return {sha256(s.encode()).hexdigest() for s in (source, source.replace('\n', '\r\n'))}


@lru_cache(maxsize=4)
def bundled_pdf(kind):
    title, body = bundled_text(kind).strip().split('\n', 1)
    result = BytesIO()
    document = SimpleDocTemplate(result, pagesize=A4, leftMargin=23*mm, rightMargin=23*mm,
                                 topMargin=26*mm, bottomMargin=23*mm,
                                 title=title, author='ИП Жвакин Александр Николаевич', invariant=True)
    heading = ParagraphStyle('heading', fontName='GateBot-Bold', fontSize=22, leading=27,
                             textColor=INK, spaceAfter=15, keepWithNext=True)
    section = ParagraphStyle('section', fontName='GateBot-Bold', fontSize=13, leading=17,
                             textColor=INK, spaceBefore=6, spaceAfter=4, keepWithNext=True)
    paragraph = ParagraphStyle('paragraph', fontName='GateBot-Regular', fontSize=10.5,
                               leading=15.3, spaceAfter=7, textColor=INK,
                               allowOrphans=False, allowWidows=False)
    story = [Paragraph(escape(title), heading)]
    for block in body.splitlines():
        line = block.strip()
        if line:
            style = section if re.match(r'^\d+\.\s', line) else paragraph
            story.append(Paragraph(escape(line), style))

    def frame(canvas, doc):
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.setFont('GateBot-Bold', 11)
        canvas.drawString(23*mm, height-17*mm, 'GateBot')
        canvas.setStrokeColor(HexColor('#C1BCDC'))
        canvas.line(23*mm, height-20*mm, width-23*mm, height-20*mm)
        canvas.setFont('GateBot-Regular', 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(23*mm, 13*mm, 'ИП Жвакин Александр Николаевич  ·  Тверь')
        canvas.drawRightString(width-23*mm, 13*mm, str(doc.page))
        canvas.restoreState()

    document.build(story, onFirstPage=frame, onLaterPages=frame)
    return result.getvalue()


if __name__ == '__main__':
    import argparse
    from zipfile import ZipFile, ZIP_DEFLATED
    parser = argparse.ArgumentParser(description='Сформировать четыре PDF из текстов комплекта GateBot')
    parser.add_argument('directory', type=Path)
    destination = parser.parse_args().directory
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination / 'GateBot-documents.zip', 'w', compression=ZIP_DEFLATED) as archive:
        for name in DOCUMENTS:
            filename = f'GateBot-{name}.pdf'
            data = bundled_pdf(name)
            (destination / filename).write_bytes(data)
            archive.writestr(filename, data)
            archive.writestr(f'GateBot-{name}.txt', bundled_text(name).encode('utf-8'))
