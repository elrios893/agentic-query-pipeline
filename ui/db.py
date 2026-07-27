import sqlite3
import os
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent / 'items.db'


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                titulo TEXT NOT NULL,
                ruta TEXT NOT NULL,
                pregunta TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        ''')


def guardar_item(tipo: str, titulo: str, ruta: str, pregunta: str = ''):
    # Evitar duplicados: misma ruta + mismo tipo
    with _get_conn() as conn:
        existente = conn.execute(
            'SELECT id FROM items WHERE ruta = ? AND tipo = ?', (ruta, tipo)
        ).fetchone()
        if existente:
            return existente['id']
        cur = conn.execute(
            'INSERT INTO items (tipo, titulo, ruta, pregunta, created_at) VALUES (?, ?, ?, ?, ?)',
            (tipo, titulo, ruta, pregunta, datetime.now().isoformat(timespec='seconds')),
        )
        return cur.lastrowid


def listar_items(tipo: str):
    with _get_conn() as conn:
        rows = conn.execute(
            'SELECT * FROM items WHERE tipo = ? ORDER BY created_at DESC', (tipo,)
        ).fetchall()
        return [dict(r) for r in rows]


def obtener_item(item_id: int):
    with _get_conn() as conn:
        r = conn.execute('SELECT * FROM items WHERE id = ?', (item_id,)).fetchone()
        return dict(r) if r else None
