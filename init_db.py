import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "news.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS news (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id TEXT UNIQUE,
        title TEXT NOT NULL,
        publish_time TEXT,
        source TEXT,
        crawler_entry TEXT,
        category TEXT,
        url TEXT,
        summary TEXT,
        content TEXT,
        raw_source_type TEXT,
        file_path TEXT,
        created_at TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        gender TEXT,
        age INTEGER,
        city TEXT,
        register_time TEXT,
        device_type TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS news_clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        news_id INTEGER NOT NULL,
        click_time TEXT NOT NULL,
        stay_seconds INTEGER,
        channel TEXT,
        device_type TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (news_id) REFERENCES news(id)
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        news_id INTEGER NOT NULL,
        comment_text TEXT NOT NULL,
        comment_time TEXT NOT NULL,
        sentiment TEXT,
        likes_count INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (news_id) REFERENCES news(id)
    );
    """)

    conn.commit()
    conn.close()
    print("数据库表初始化完成。")

if __name__ == "__main__":
    init_db()