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

    def discard_unreferenced(old_url, manager):
        match = re.fullmatch(re.escape(URL_PREFIX) + r'([a-f0-9]{32}\.png)', old_url or '')
        if match and not any(b.logo == old_url for b in manager.get_all_branches()):
            try:
                (image_directory() / match[1]).unlink(missing_ok=True)
            except OSError:
                # The committed new logo remains usable if cleanup is deferred.
                logger.event('BRANCH_LOGO_CLEANUP_DEFERRED', level='WARNING')

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.post('/admin/api/branches/<int:branch_id>/logo')
    def upload(branch_id):
        manager = authorize(branch_id)
        request.max_content_length = MAX_UPLOAD_BYTES + 65536
        uploaded = request.files.get('image')
        if uploaded is None:
            raise ValueError('Выберите файл логотипа')
        pixels = canonical_image(uploaded.stream.read(MAX_UPLOAD_BYTES + 1))
        identifier = secrets.token_hex(16)
        filename = identifier + '.png'
        directory = image_directory()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
        url = URL_PREFIX + filename
        created = False
        try:
            with destination.open('xb') as output:
                created = True
                output.write(pixels)
                output.flush()
                os.fsync(output.fileno())
            with manager.store.transaction() as branches:
                authorize(branch_id)  # Role and active branch can change during decoding.
                branch = branches.get(str(branch_id))
                if branch is None:
                    abort(404)
                old_url = branch.get('logo', '')
                branch['logo'] = url
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        discard_unreferenced(old_url, manager)
        logger.event('BRANCH_LOGO_UPDATED', branch_id=branch_id, actor=session.get('admin_username'))
        return jsonify(status='success', logo=url)

    @bp.delete('/admin/api/branches/<int:branch_id>/logo')
    def remove(branch_id):
        manager = authorize(branch_id)
        with manager.store.transaction() as branches:
            authorize(branch_id)
            branch = branches.get(str(branch_id))
            if branch is None:
                abort(404)
            old_url = branch.get('logo', '')
            branch['logo'] = ''
        discard_unreferenced(old_url, manager)
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
