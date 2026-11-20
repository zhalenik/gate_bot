"""Published subscription guide. Text and headings are always rendered escaped."""
from pathlib import Path


def sections(document):
    if not isinstance(document, str) or not document.strip() or len(document) > 32000:
        raise ValueError('Введите текст правил длиной до 32 000 символов')
    parts = []
    for line in document.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('## ') and line[3:].strip():
            parts.append((line[3:].strip(), []))
        elif parts:
            parts[-1][1].append(line)
        else:
            raise ValueError('Начните раздел с ## и заголовка')
    if not parts or any(not paragraphs for _, paragraphs in parts):
        raise ValueError('Добавьте текст под каждым заголовком')
    return parts


def read(db):
    db.execute('CREATE TABLE IF NOT EXISTS published_billing_rules (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, document TEXT NOT NULL)')
    row = db.execute('SELECT revision,document FROM published_billing_rules WHERE id=1').fetchone()
    if row:
        return row['revision'], row['document']
    return 0, (Path(__file__).parent/'resources'/'billing_rules.txt').read_text(encoding='utf-8')


def publish(db, revision, document):
    sections(document)
    current, _ = read(db)
    if str(current) != revision:
        raise ValueError('Правила изменились. Обновите страницу перед публикацией.')
    db.execute('INSERT OR REPLACE INTO published_billing_rules VALUES (1,?,?)', (current+1, document))
