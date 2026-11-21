"""Public immutable editions and the installation owner's document library."""
from io import BytesIO

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_file, session

from legal_documents import ADMIN_DOCS, CLIENT_DOCS, MAX_SIZE, TITLES, current, publish
from legal_pdf import bundled_pdf, bundled_text_hashes


def create_blueprint(provider):
    bp = Blueprint('legal', __name__)

    @bp.app_template_global()
    def registration_documents(audience):
        with provider()['service'].product.connect() as db:
            return current(db,CLIENT_DOCS if audience=='client' else ADMIN_DOCS)

    def owner():
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor:
            abort(401)
        if not actor.get('is_super'):
            abort(403)
        return services

    @bp.get('/documents/<kind>/<int:edition>')
    def edition(kind, edition):
        with provider()['service'].product.connect() as db:
            row = db.execute('SELECT media,content,digest FROM registration_documents WHERE kind=? AND id=?',(kind,edition)).fetchone()
        if row is None:
            abort(404)
        download = request.args.get('download')
        if download in ('pdf', 'original'):
            data, media = row['content'], row['media']
            if download == 'pdf' and media != 'application/pdf':
                if row['digest'] not in bundled_text_hashes(kind):
                    abort(404)
                data, media = bundled_pdf(kind), 'application/pdf'
            extension = 'pdf' if media == 'application/pdf' else 'txt'
            return send_file(BytesIO(data), mimetype=media, as_attachment=True,
                             download_name=f'GateBot-{kind}-{edition}.{extension}')
        return Response(row['content'],mimetype=row['media'])

    @bp.get('/admin/documents')
    def library():
        services = owner()
        with services['service'].product.connect() as db:
            rows = db.execute('SELECT id,kind,filename,media,digest,published FROM registration_documents ORDER BY id DESC').fetchall()
        return render_template('admin/documents.html', admin_page=True, is_super=True, titles=TITLES,
                               versions=rows, text_hashes={kind:bundled_text_hashes(kind) for kind in TITLES},
                               offer=services['billing'].catalog.offer())

    @bp.post('/admin/documents/<kind>')
    def upload(kind):
        services = owner()
        request.max_content_length = MAX_SIZE + 65536
        file = request.files.get('document')
        try:
            if file is None:
                raise ValueError('Выберите файл')
            with services['service'].product.connect() as db:
                publish(db,kind,file.filename,file.stream.read(MAX_SIZE+1),session['admin_username'])
        except ValueError as error:
            flash(str(error),'error')
        else:
            flash('Редакция документа опубликована.','success')
        return redirect('/admin/documents',code=303)

    return bp
