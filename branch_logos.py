"""Scoped image uploads. Public logo files contain re-encoded pixels only."""
import io
import os
from pathlib import Path
import re
import secrets
import warnings

from flask import Blueprint, abort, jsonify, request, send_file, session
from PIL import Image, ImageOps, UnidentifiedImageError

import logger


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
URL_PREFIX = '/media/branch-logos/'


def canonical_image(data):
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('Выберите изображение размером до 5 МБ')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError('Поддерживаются PNG, JPEG и WebP')
                width, height = source.size
                if not width or not height or width > 4096 or height > 4096 or width * height > MAX_PIXELS:
                    raise ValueError('Максимальный размер изображения — 4096 × 4096')
                if getattr(source, 'n_frames', 1) != 1:
                    raise ValueError('Выберите неподвижное изображение')
                source.load()
                pixels = ImageOps.exif_transpose(source).convert('RGBA')
                pixels.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                # Create a clean image: no EXIF, profiles, text chunks or trailing
                # uploaded bytes are copied to the public file.
                clean = Image.new('RGBA', pixels.size)
                clean.paste(pixels)
                clean.save(output, format='PNG', optimize=True)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ValueError('Не удалось прочитать изображение. Выберите PNG, JPEG или WebP') from None


def create_blueprint(provider):
    bp = Blueprint('branch_logos', __name__)

    def services():
        return provider()

    def image_directory():
        return Path(services()['branches'].branches_file).parent / 'branch-logos'

    def authorize(branch_id):
        current = services()['auth'].get_current_admin()
        if not current:
            abort(401)
        # Same active-branch rule as the surrounding branch settings form.
        if current.get('is_super') is not True and str(current.get('branch_id')) != str(branch_id):
            abort(403)
        manager = services()['branches']
        if manager.get_branch(branch_id) is None:
            abort(404)
        return manager

    def discard_unreferenced(branch_id, old_url, manager):
        match = re.fullmatch(re.escape(URL_PREFIX) + r'([a-f0-9]{32}\.png)', old_url or '')
        if match:
            try:
                path=image_directory()/match[1]
                path.with_suffix(f'.b{branch_id}.source.png').unlink(missing_ok=True)
                if not any(b.logo == old_url for b in manager.get_all_branches()):
                    path.unlink(missing_ok=True)
            except OSError:
                # The committed new logo remains usable if cleanup is deferred.
                logger.event('BRANCH_LOGO_CLEANUP_DEFERRED', level='WARNING')

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    def source_path(branch_id, url):
        match=re.fullmatch(re.escape(URL_PREFIX) + r'([a-f0-9]{32}\.png)', url or '')
        if match:
            display=image_directory()/match[1]
            source=display.with_suffix(f'.b{branch_id}.source.png')
            return source if source.is_file() else display

    def editor(branch_id, branch):
        from branding_images import LOGO_FRAME
        url=branch.get('logo','')
        return {'logo':url, 'frame':branch.get('logo_frame',LOGO_FRAME),
                'source_url':f'/admin/api/branches/{branch_id}/logo/source?v={Path(url).stem}' if source_path(branch_id,url) else None}

    def check_expected(current, expected):
        if current != expected:
            abort(409,description='Логотип уже изменён. Обновите страницу.')

    @bp.get('/admin/api/branches/<int:branch_id>/logo')
    def read(branch_id):
        return jsonify(editor(branch_id,authorize(branch_id).load_branches()[str(branch_id)]))

    @bp.get('/admin/api/branches/<int:branch_id>/logo/source')
    def source(branch_id):
        path=source_path(branch_id,authorize(branch_id).get_branch(branch_id).logo)
        if not path or not path.is_file(): abort(404)
        response=send_file(path,mimetype='image/png')
        response.headers['Cache-Control']='private, no-store'
        return response

    @bp.post('/admin/api/branches/<int:branch_id>/logo')
    def upload(branch_id):
        from branding_images import LOGO_FRAME, canonical_brand, icon_png, submitted_frame
        manager = authorize(branch_id)
        request.max_content_length = MAX_UPLOAD_BYTES + 65536
        uploaded = request.files.get('image')
        current=manager.get_branch(branch_id).logo
        expected=request.form.get('expected_logo',current)
        check_expected(current,expected)
        frame=submitted_frame(request.form)
        if uploaded and uploaded.filename:
            _, _, pixels=canonical_brand(uploaded.stream.read(MAX_UPLOAD_BYTES + 1))
        else:
            path=source_path(branch_id,current)
            if not frame or not path:
                raise ValueError('Выберите файл логотипа')
            try:
                pixels=path.read_bytes()
            except FileNotFoundError:
                branch=manager.get_branch(branch_id)
                if branch is None: abort(404)
                check_expected(branch.logo,expected)
                raise ValueError('Загрузите исходное изображение ещё раз') from None
        frame=frame or LOGO_FRAME
        rendered=pixels if frame==LOGO_FRAME else icon_png(pixels,None,**frame)
        identifier = secrets.token_hex(16)
        filename = identifier + '.png'
        directory = image_directory()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
        url = URL_PREFIX + filename
        created = []
        try:
            files={destination:rendered}
            if rendered != pixels:
                files[destination.with_suffix(f'.b{branch_id}.source.png')]=pixels
            for path,content in files.items():
                with path.open('xb') as output:
                    created.append(path)
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            with manager.store.transaction() as branches:
                authorize(branch_id)  # Role and active branch can change during decoding.
                branch = branches.get(str(branch_id))
                if branch is None:
                    abort(404)
                old_url = branch.get('logo', '')
                check_expected(old_url,expected)
                branch['logo'] = url
                branch['logo_frame'] = frame
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        discard_unreferenced(branch_id, old_url, manager)
        logger.event('BRANCH_LOGO_UPDATED', branch_id=branch_id, actor=session.get('admin_username'))
        return jsonify(status='success', **editor(branch_id,branch))

    @bp.delete('/admin/api/branches/<int:branch_id>/logo')
    def remove(branch_id):
        manager = authorize(branch_id)
        data=request.get_json() if request.is_json else request.form
        if not hasattr(data,'get'):
            raise ValueError('Проверьте параметры удаления логотипа')
        expected=data.get('expected_logo',manager.get_branch(branch_id).logo)
        with manager.store.transaction() as branches:
            authorize(branch_id)
            branch = branches.get(str(branch_id))
            if branch is None:
                abort(404)
            old_url = branch.get('logo', '')
            check_expected(old_url,expected)
            branch['logo'] = ''
            branch.pop('logo_frame',None)
        discard_unreferenced(branch_id, old_url, manager)
        logger.event('BRANCH_LOGO_REMOVED', branch_id=branch_id, actor=session.get('admin_username'))
        return jsonify(status='success', logo='')

    @bp.get('/media/branch-logos/<filename>')
    def public_image(filename):
        if not re.fullmatch(r'[a-f0-9]{32}\.png', filename):
            abort(404)
        # Logos are public branding, with unguessable URLs. Serve only a current
        # branch logo, never another private file or an abandoned upload.
        manager = services()['branches']
        if not any(b.logo == URL_PREFIX + filename for b in manager.get_all_branches()):
            abort(404)
        path = image_directory() / filename
        if not path.is_file():
            abort(404)
        response = send_file(path, mimetype='image/png', download_name='branch-logo.png')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    return bp
