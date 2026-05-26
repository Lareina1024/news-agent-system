import json
import sqlite3
import hashlib
from pathlib import Path
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("DB_PATH", "news.db"))
NEWS_JSONL = Path(os.getenv("NEWS_JSONL", "crawler/news_db.jsonl"))

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_content_hash(title: str, content: str) -> str:
    raw = f"{title or ''}|{content or ''}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

def init_db(conn):
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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        content_hash TEXT,
        retrieval_text TEXT,
        kb_synced INTEGER DEFAULT 0,
        kb_synced_at TEXT,
        kb_doc_id TEXT
    )
    """)

    # 兼容旧表：如果之前已经有 news 表，就补字段
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(news)").fetchall()]
    add_columns = {
        "content_hash": "TEXT",
        "retrieval_text": "TEXT",
        "kb_synced": "INTEGER DEFAULT 0",
        "kb_synced_at": "TEXT",
        "kb_doc_id": "TEXT"
    }

    for col, col_type in add_columns.items():
        if col not in columns:
            cursor.execute(f"ALTER TABLE news ADD COLUMN {col} {col_type}")

    conn.commit()


def build_retrieval_text(item):
    if item.get("retrieval_text"):
        return item["retrieval_text"]

    return f"""标题：{item.get("title", "")}
发布时间：{item.get("publish_time", "")}
来源：{item.get("source", "")}
抓取入口：{item.get("fetch_source", "")}
分类：{item.get("category", "")}
链接：{item.get("url", "")}

摘要：
{item.get("summary", "")}

正文：
{item.get("content", "")}
""".strip()


def normalize_item(item):
    extra = item.get("extra") or {}

    article_id = (
        extra.get("article_id")
        or item.get("article_id")
        or item.get("id")
    )

    title = item.get("title") or ""
    content = item.get("content") or ""
    retrieval_text = build_retrieval_text(item)
    content_hash = item.get("content_hash") or make_content_hash(title, content)

    return {
        "article_id": article_id,
        "title": title,
        "publish_time": item.get("publish_time"),
        "source": item.get("source"),
        "crawler_entry": item.get("fetch_source") or item.get("crawler_entry"),
        "category": item.get("category"),
        "url": item.get("url"),
        "summary": item.get("summary"),
        "content": content,
        "raw_source_type": extra.get("raw_source_type") or item.get("raw_source_type"),
        "file_path": item.get("file_path"),
        "content_hash": content_hash,
        "retrieval_text": retrieval_text
    }


def import_jsonl(mark_old_as_synced=False):
    if not NEWS_JSONL.exists():
        raise FileNotFoundError(f"找不到文件：{NEWS_JSONL}")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    cursor = conn.cursor()

    inserted = 0
    ignored = 0
    updated = 0

    with NEWS_JSONL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            item = json.loads(line)
            news = normalize_item(item)

            if not news["article_id"] or not news["title"]:
                ignored += 1
                continue

            # 历史 1k 条如果你已经手动传过 FastGPT，就设为 1
            kb_synced_value = 1 if mark_old_as_synced else 0

            cursor.execute("""
            INSERT OR IGNORE INTO news (
                article_id, title, publish_time, source, crawler_entry,
                category, url, summary, content, raw_source_type, file_path,
                content_hash, retrieval_text, kb_synced
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                news["article_id"],
                news["title"],
                news["publish_time"],
                news["source"],
                news["crawler_entry"],
                news["category"],
                news["url"],
                news["summary"],
                news["content"],
                news["raw_source_type"],
                news["file_path"],
                news["content_hash"],
                news["retrieval_text"],
                kb_synced_value
            ))

            if cursor.rowcount == 1:
                inserted += 1
            else:
                # 已存在的新闻，补充 retrieval_text/content_hash，但不要覆盖 kb_synced
                cursor.execute("""
                UPDATE news
                SET title = ?,
                    publish_time = ?,
                    source = ?,
                    crawler_entry = ?,
                    category = ?,
                    url = ?,
                    summary = ?,
                    content = ?,
                    raw_source_type = ?,
                    content_hash = ?,
                    retrieval_text = ?
                WHERE article_id = ?
                """, (
                    news["title"],
                    news["publish_time"],
                    news["source"],
                    news["crawler_entry"],
                    news["category"],
                    news["url"],
                    news["summary"],
                    news["content"],
                    news["raw_source_type"],
                    news["content_hash"],
                    news["retrieval_text"],
                    news["article_id"]
                ))
                updated += 1

    conn.commit()
    conn.close()

    print(f"导入完成：新增 {inserted}，更新 {updated}，跳过 {ignored}")
    print(f"数据库：{DB_PATH}")


if __name__ == "__main__":
    import_jsonl(mark_old_as_synced=False)