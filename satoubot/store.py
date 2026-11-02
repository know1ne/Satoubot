import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                filename TEXT NOT NULL, size INTEGER NOT NULL, path TEXT,
                thumbnail TEXT, state TEXT NOT NULL, target TEXT,
                telegram_message_id INTEGER, error TEXT, approval_token TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);
        ''')
        self.db.commit()

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
        self.db.commit()

    def create(self, source, title, filename, size):
        cursor = self.db.execute('INSERT INTO jobs(source,title,filename,size,state) VALUES (?,?,?,?,?)',
                                 (source,title,filename,size,'ready'))
        self.db.commit()
        return cursor.lastrowid

    def get(self, job_id):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    def update(self, job_id, **changes):
        allowed = {'title','filename','path','thumbnail','state','target','telegram_message_id','error','approval_token'}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError('invalid update')
        fields = ','.join(f'{key}=?' for key in changes)
        self.db.execute(f'UPDATE jobs SET {fields} WHERE id=?', (*changes.values(), job_id))
        self.db.commit()

    def find_source(self, source):
        return self.db.execute("SELECT * FROM jobs WHERE source=? AND state!='failed' ORDER BY id DESC LIMIT 1", (source,)).fetchone()

    def search(self, query):
        return self.db.execute("SELECT * FROM jobs WHERE state='published' AND title LIKE ? ORDER BY title LIMIT 20", ('%' + query + '%',)).fetchall()
