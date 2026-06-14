import sqlite3
import os

DB_PATH = "/app/data/music.db"

db = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = db.cursor()


def init_db():
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS songs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        message_id INTEGER,
        source_group_id INTEGER,
        source_username TEXT
    );

    CREATE TABLE IF NOT EXISTS playlists(
        user_id INTEGER,
        song_id INTEGER,
        PRIMARY KEY(user_id, song_id)
    );

    CREATE TABLE IF NOT EXISTS top_searches(
        query TEXT PRIMARY KEY,
        count INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS sources(
        group_id INTEGER,
        topic_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS topic_links(
        keyword TEXT PRIMARY KEY,
        link TEXT
    );
    """)
     cur.executescript("""
    CREATE TABLE IF NOT EXISTS stats(
        key TEXT PRIMARY KEY,
        value INTEGER DEFAULT 0
    );
    """)

db.commit()
