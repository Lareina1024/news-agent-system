import os
import requests
import sqlite3
from dotenv import load_dotenv

load_dotenv()

FASTGPT_BASE_URL = os.getenv("FASTGPT_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
API_URL = f"{FASTGPT_BASE_URL}/api/v1/chat/completions"

API_KEY = os.getenv("TEXT2SQL_API_KEY")
APP_ID = os.getenv("TEXT2SQL_APP_ID")

DB_PATH = os.getenv("DB_PATH", "news.db")

if not API_KEY:
    raise RuntimeError("缺少 TEXT2SQL_API_KEY")

if not APP_ID:
    raise RuntimeError("缺少 TEXT2SQL_APP_ID")

def call_fastgpt(question: str) -> str:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    system_prompt = """
你是一个 SQLite SQL 生成助手。

数据库中只有一张表：news

表字段如下：
- id
- article_id
- title
- publish_time
- source
- crawler_entry
- category
- url
- summary
- content
- raw_source_type
- file_path
- created_at

要求：
1. 只输出一条可执行的 SQLite SQL
2. 不要输出解释，不要输出 markdown 代码块
3. 只能使用 news 表和上述字段
4. 列表查询默认返回 title, publish_time, source
5. 列表查询默认按 publish_time 倒序
6. 列表查询默认 LIMIT 10
7. 统计类问题输出聚合 SQL
8. 如果无法生成合法 SQL，只输出 ERROR
"""

    payload = {
        "chatId": "text2sql-test",
        "stream": False,
        "detail": False,
        "variables": {
            "appId": APP_ID
        },
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question}
        ]
    }

    response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()

    sql = data["choices"][0]["message"]["content"].strip()
    return sql


def clean_sql(sql: str) -> str:
    sql = sql.strip()

    if sql.startswith("```sql"):
        sql = sql.replace("```sql", "", 1).strip()
    if sql.startswith("```"):
        sql = sql.replace("```", "", 1).strip()
    if sql.endswith("```"):
        sql = sql[:-3].strip()

    return sql


def execute_sql(sql: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return columns, rows, None
    except Exception as e:
        return None, None, str(e)
    finally:
        conn.close()


def print_result(columns, rows):
    print("\n查询结果：")
    print("字段：", columns)
    for i, row in enumerate(rows, 1):
        print(f"{i}. {row}")


if __name__ == "__main__":
    question = input("请输入问题：").strip()

    try:
        sql = call_fastgpt(question)
        sql = clean_sql(sql)

        print("\n模型生成的 SQL：")
        print(sql)

        if sql == "ERROR":
            print("\n模型无法生成 SQL")
        else:
            columns, rows, err = execute_sql(sql)
            if err:
                print("\nSQL 执行失败：", err)
            else:
                print_result(columns, rows)

    except Exception as e:
        print("调用 FastGPT 失败：", e)