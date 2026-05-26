import os
import sqlite3
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "/Users/xuruohan/Desktop/news-agent/news.db")
FASTGPT_BASE_URL = os.getenv("FASTGPT_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
FASTGPT_API_KEY = os.getenv("FASTGPT_API_KEY")
FASTGPT_DATASET_ID = os.getenv("FASTGPT_DATASET_ID")

SYNC_LIMIT = 5


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_unsynced_news(conn, limit=5):
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, article_id, title, retrieval_text, summary, content
    FROM news
    WHERE kb_synced = 0
      AND title IS NOT NULL
      AND title != ''
      AND (
          retrieval_text IS NOT NULL
          OR content IS NOT NULL
      )
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))
    return cursor.fetchall()


def build_text(title, retrieval_text, summary, content):
    if retrieval_text and retrieval_text.strip():
        return retrieval_text.strip()

    return f"""标题：{title}

摘要：
{summary or ""}

正文：
{content or ""}
""".strip()


def upload_text_to_fastgpt(title, text, article_id):
    if not FASTGPT_API_KEY:
        raise RuntimeError("缺少 FASTGPT_API_KEY，请检查 .env")
    if not FASTGPT_DATASET_ID:
        raise RuntimeError("缺少 FASTGPT_DATASET_ID，请检查 .env")

    url = f"{FASTGPT_BASE_URL}/api/core/dataset/collection/create/text"

    headers = {
        "Authorization": f"Bearer {FASTGPT_API_KEY}",
        "Content-Type": "application/json"
    }


    payload = {
        "text": text,
        "datasetId": FASTGPT_DATASET_ID,
        "parentId": None,
        "name": title[:80],

        "trainingType": "chunk",
        "chunkSettingMode": "custom",
        "chunkSize": 800,
        "chunkSplitter": "",

        "qaPrompt": "",
        "metadata": {
            "article_id": article_id,
            "source": "tencent_news_crawler"
        }
    }

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    print("FastGPT返回：", data)

    if data.get("code") not in (0, 200):
        raise RuntimeError(f"FastGPT 上传失败：{data}")

    collection_id = (
        data.get("data", {}).get("collectionId")
        or data.get("data")
    )

    return collection_id, data


def mark_synced(conn, news_id, collection_id):
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE news
    SET kb_synced = 1,
        kb_synced_at = ?,
        kb_doc_id = ?
    WHERE id = ?
    """, (now_str(), collection_id, news_id))
    conn.commit()


def main():
    conn = sqlite3.connect(DB_PATH)

    rows = get_unsynced_news(conn, SYNC_LIMIT)

    if not rows:
        print("没有需要同步到 FastGPT 的新闻。")
        conn.close()
        return

    print(f"发现 {len(rows)} 条待同步新闻")

    success = 0
    failed = 0

    for row in rows:
        news_id, article_id, title, retrieval_text, summary, content = row
        text = build_text(title, retrieval_text, summary, content)

        try:
            collection_id, result = upload_text_to_fastgpt(
                title=title,
                text=text,
                article_id=article_id
            )
            mark_synced(conn, news_id, collection_id)
            success += 1
            print(f"[OK] {title} -> collectionId={collection_id}")
        except Exception as e:
            failed += 1
            print(f"[ERROR] {title} 上传失败：{e}")

    conn.close()
    print(f"同步完成：成功 {success}，失败 {failed}")


if __name__ == "__main__":
    main()