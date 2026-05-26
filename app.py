import json
import logging
import os
import sqlite3
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, Response
from pyecharts import options as opts
from pyecharts.charts import Bar, Line, Pie

load_dotenv()

app = Flask(__name__)

FASTGPT_BASE_URL = os.getenv("FASTGPT_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
API_URL = f"{FASTGPT_BASE_URL}/api/v1/chat/completions"

TEXT2SQL_API_KEY = os.getenv("TEXT2SQL_API_KEY")
TEXT2SQL_APP_ID = os.getenv("TEXT2SQL_APP_ID")

RAG_API_KEY = os.getenv("RAG_API_KEY")
RAG_APP_ID = os.getenv("RAG_APP_ID")

DB_PATH = os.getenv("DB_PATH", "/Users/xuruohan/Desktop/news-agent/news.db")

REQUEST_TIMEOUT = 60
DEFAULT_PORT = int(os.getenv("FLASK_PORT", "5001"))
LOG_FILE = os.getenv("LOG_FILE", "news_agent.log")

ALLOWED_SQL_PREFIX = ("SELECT", "WITH")
FORBIDDEN_SQL_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "REPLACE", "ATTACH", "DETACH", "PRAGMA", "VACUUM"
]

SQL_CACHE_SIZE = 100
_SQL_CACHE: "OrderedDict[str, str]" = OrderedDict()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# =========================
# 固定运营问题配置
# 如果你的 news 表字段名不同，主要改这里
# =========================

PRESET_SQL: Dict[str, Dict[str, str]] = {
    "publish_7days": {
        "name": "最近7天发布趋势",
        "sql": """
            SELECT DATE(publish_time) AS 日期, COUNT(*) AS 新闻数量
            FROM news
            WHERE DATE(publish_time) >= DATE((SELECT MAX(DATE(publish_time)) FROM news), '-6 day')
            GROUP BY DATE(publish_time)
            ORDER BY DATE(publish_time)
        """
    },
    "comment_top10": {
        "name": "评论TOP10",
        "sql": """
            SELECT n.title AS 新闻标题, COUNT(c.id) AS 评论数
            FROM news n
            LEFT JOIN comments c ON n.id = c.news_id
            GROUP BY n.id, n.title
            ORDER BY 评论数 DESC
            LIMIT 10
        """
    },
    "click_top10": {
        "name": "点击TOP10",
        "sql": """
            SELECT n.title AS 新闻标题, COUNT(nc.id) AS 点击量
            FROM news n
            LEFT JOIN news_clicks nc ON n.id = nc.news_id
            GROUP BY n.id, n.title
            ORDER BY 点击量 DESC
            LIMIT 10
        """
    },
    "category_count": {
        "name": "各分类新闻数量",
        "sql": """
            SELECT category AS 新闻分类, COUNT(*) AS 新闻数量
            FROM news
            GROUP BY category
            ORDER BY 新闻数量 DESC
        """
    },
    "source_count": {
        "name": "各来源新闻数量",
        "sql": """
            SELECT source AS 新闻来源, COUNT(*) AS 新闻数量
            FROM news
            GROUP BY source
            ORDER BY 新闻数量 DESC
        """
    },
    "low_click_news": {
        "name": "低表现新闻",
        "sql": """
            SELECT n.title AS 新闻标题, COUNT(nc.id) AS 点击量
            FROM news n
            LEFT JOIN news_clicks nc ON n.id = nc.news_id
            GROUP BY n.id, n.title
            ORDER BY 点击量 ASC
            LIMIT 10
        """
    },
    "high_interaction_news": {
        "name": "高互动新闻",
        "sql": """
            SELECT 
                n.title AS 新闻标题,
                COUNT(DISTINCT nc.id) + COUNT(DISTINCT c.id) AS 互动量
            FROM news n
            LEFT JOIN news_clicks nc ON n.id = nc.news_id
            LEFT JOIN comments c ON n.id = c.news_id
            GROUP BY n.id, n.title
            ORDER BY 互动量 DESC
            LIMIT 10
        """
    }
}

PRESET_RAG: Dict[str, Dict[str, str]] = {
    "latest_hot_summary": {
        "name": "最新一天热点总结",
        "question": "基于数据库中最新一天的新闻，生成热点总结。"
    },
    "latest_daily_report": {
        "name": "最新一天运营日报",
        "question": "基于数据库中最新一天的新闻，生成运营日报。"
    }
}


def success_response(
    data: Optional[Dict[str, Any]] = None,
    msg: str = "success",
    meta: Optional[Dict[str, Any]] = None
):
    return jsonify({
        "code": 0,
        "msg": msg,
        "data": data or {},
        "meta": meta or {}
    })


def error_response(
    msg: str,
    code: int = 1,
    http_status: int = 400,
    data: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None
):
    return jsonify({
        "code": code,
        "msg": msg,
        "data": data or {},
        "meta": meta or {}
    }), http_status


def clean_sql(sql: str) -> str:
    sql = (sql or "").strip()

    if sql.startswith("```sql"):
        sql = sql.replace("```sql", "", 1).strip()
    if sql.startswith("```"):
        sql = sql.replace("```", "", 1).strip()
    if sql.endswith("```"):
        sql = sql[:-3].strip()

    if sql.endswith(";"):
        sql = sql[:-1].strip()

    return sql


def is_safe_sql(sql: str) -> Tuple[bool, str]:
    if not sql:
        return False, "SQL 为空"

    upper_sql = sql.strip().upper()

    if not upper_sql.startswith(ALLOWED_SQL_PREFIX):
        return False, "只允许执行 SELECT / WITH 查询"

    for keyword in FORBIDDEN_SQL_KEYWORDS:
        if keyword in upper_sql:
            return False, f"SQL 包含危险关键字: {keyword}"

    if ";" in sql:
        return False, "不允许执行多条 SQL"

    return True, ""


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = None
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-20000;")
    return conn


def get_cached_sql(question: str) -> Optional[str]:
    key = question.strip().lower()
    sql = _SQL_CACHE.get(key)
    if sql is not None:
        _SQL_CACHE.move_to_end(key)
    return sql


def set_cached_sql(question: str, sql: str) -> None:
    key = question.strip().lower()
    _SQL_CACHE[key] = sql
    _SQL_CACHE.move_to_end(key)
    while len(_SQL_CACHE) > SQL_CACHE_SIZE:
        _SQL_CACHE.popitem(last=False)


def execute_sql(sql: str) -> Tuple[List[str], List[List[Any]], Optional[str]]:
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return columns, [list(row) for row in rows], None
    except Exception as e:
        return [], [], str(e)
    finally:
        conn.close()


def normalize_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def detect_chart_type(columns: List[str], rows: List[List[Any]]) -> Optional[str]:
    if not columns or not rows:
        return None

    if len(columns) == 2:
        first_col = columns[0].lower()
        second_col = columns[1].lower()

        time_keywords = ["time", "date", "day", "month", "publish", "日期", "时间"]
        count_keywords = ["count", "total", "num", "click", "comment", "likes", "数量", "点击", "评论"]

        is_time_series = any(k in first_col for k in time_keywords)
        is_metric = any(k in second_col for k in count_keywords)

        if is_time_series and is_metric:
            return "line"

        if len(rows) <= 8:
            return "pie"

        return "bar"

    if len(columns) >= 2:
        return "bar"

    return None


def build_chart_option(columns: List[str], rows: List[List[Any]]) -> Optional[Dict[str, Any]]:
    chart_type = detect_chart_type(columns, rows)
    if not chart_type or len(columns) < 2 or not rows:
        return None

    x_data = [str(normalize_value(row[0])) for row in rows]
    y_data = []

    for row in rows:
        value = row[1]
        try:
            y_data.append(float(value))
        except (TypeError, ValueError):
            return None

    title = "查询结果可视化"

    if chart_type == "bar":
        chart = (
            Bar()
            .add_xaxis(x_data)
            .add_yaxis(columns[1], y_data)
            .set_global_opts(
                title_opts=opts.TitleOpts(title=title),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=25)),
                datazoom_opts=[opts.DataZoomOpts()]
            )
        )
        return {"type": "bar", "title": title, "option": json.loads(chart.dump_options())}

    if chart_type == "line":
        chart = (
            Line()
            .add_xaxis(x_data)
            .add_yaxis(columns[1], y_data, is_smooth=True)
            .set_global_opts(
                title_opts=opts.TitleOpts(title=title),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=25)),
                datazoom_opts=[opts.DataZoomOpts()]
            )
        )
        return {"type": "line", "title": title, "option": json.loads(chart.dump_options())}

    if chart_type == "pie":
        pairs = [list(z) for z in zip(x_data, y_data)]
        chart = (
            Pie()
            .add("", pairs)
            .set_global_opts(title_opts=opts.TitleOpts(title=title))
            .set_series_opts(label_opts=opts.LabelOpts(formatter="{b}: {c}"))
        )
        return {"type": "pie", "title": title, "option": json.loads(chart.dump_options())}

    return None


def log_query(
    question: str,
    route: str,
    success: bool,
    cost_ms: int,
    row_count: int = 0,
    sql: str = "",
    error: str = "",
    model_cached: bool = False
):
    logger.info(json.dumps({
        "question": question,
        "route": route,
        "sql": sql,
        "success": success,
        "cost_ms": cost_ms,
        "row_count": row_count,
        "error": error,
        "model_cached": model_cached
    }, ensure_ascii=False))


def call_text2sql_model(question: str) -> Tuple[str, bool]:
    cached_sql = get_cached_sql(question)
    if cached_sql:
        return cached_sql, True

    headers = {
        "Authorization": f"Bearer {TEXT2SQL_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
你是新闻运营数据库 Text2SQL 助手。

要求：
1. 只生成 SQLite SQL。
2. 只能生成 SELECT 或 WITH 查询。
3. 不要解释，不要 Markdown，不要代码块。
4. 禁止 INSERT、UPDATE、DELETE、DROP、ALTER、CREATE。
5. 如果无法生成 SQL，只返回 ERROR。

真实数据库结构：
1. news：新闻基础表
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

2. comments：评论表
- id
- user_id
- news_id
- comment_text
- comment_time
- sentiment

3. news_clicks：点击表
- id
- user_id
- news_id
- click_time

说明：
- 点击量需要从 news_clicks 表按 news_id 统计 COUNT。
- 评论数需要从 comments 表按 news_id 统计 COUNT。
- news 表没有 click_count 和 comment_count 字段，不要使用这两个字段。

用户问题：
{question}
"""

    payload = {
        "chatId": f"text2sql-{uuid.uuid4().hex}",
        "stream": False,
        "detail": False,
        "variables": {
            "appId": TEXT2SQL_APP_ID
        },
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    data = response.json()
    sql = data["choices"][0]["message"]["content"].strip()
    set_cached_sql(question, sql)
    return sql, False


def call_rag_model(question: str) -> str:
    headers = {
        "Authorization": f"Bearer {RAG_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "chatId": f"rag-{uuid.uuid4().hex}",
        "stream": False,
        "detail": False,
        "variables": {
            "appId": RAG_APP_ID
        },
        "messages": [
            {"role": "user", "content": question}
        ]
    }

    response = requests.post(
        API_URL,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def detect_query_route(question: str) -> str:
    q = question.strip()

    rag_keywords = [
        "总结", "概括", "介绍", "解释", "是什么", "为什么", "怎么看",
        "分析一下", "解读", "背景", "讲了什么", "主要内容", "说了什么",
        "帮我总结", "总结一下", "这篇新闻", "热点新闻", "新闻内容"
    ]

    text2sql_keywords = [
        "多少", "统计", "排行", "排名", "最高", "最低", "前10", "top",
        "点击量", "点击最多", "评论数", "评论最多", "数量", "趋势",
        "占比", "比例", "按天", "每天", "哪些", "哪个", "最受欢迎",
        "新闻发布数量", "用户", "来源", "分类"
    ]

    for kw in rag_keywords:
        if kw in q:
            return "rag"

    for kw in text2sql_keywords:
        if kw in q:
            return "text2sql"

    return "text2sql"


def handle_text2sql(question: str):
    try:
        t0 = time.time()

        raw_sql, model_cached = call_text2sql_model(question)
        t1 = time.time()

        sql = clean_sql(raw_sql)

        if sql == "ERROR":
            cost_ms = int((time.time() - t0) * 1000)
            log_query(question, "text2sql", False, cost_ms, 0, sql=sql, error="模型返回 ERROR", model_cached=model_cached)
            return error_response(
                msg="模型无法生成合法 SQL",
                code=4002,
                http_status=400,
                data={"route": "text2sql", "question": question},
                meta={"cost_ms": cost_ms, "model_cached": model_cached}
            )

        is_safe, reason = is_safe_sql(sql)
        if not is_safe:
            cost_ms = int((time.time() - t0) * 1000)
            log_query(question, "text2sql", False, cost_ms, 0, sql=sql, error=reason, model_cached=model_cached)
            return error_response(
                msg="SQL 安全校验未通过",
                code=4003,
                http_status=400,
                data={"route": "text2sql", "question": question, "sql": sql},
                meta={"reason": reason, "cost_ms": cost_ms, "model_cached": model_cached}
            )

        columns, rows, err = execute_sql(sql)
        t2 = time.time()

        if err:
            cost_ms = int((time.time() - t0) * 1000)
            log_query(question, "text2sql", False, cost_ms, 0, sql=sql, error=err, model_cached=model_cached)
            return error_response(
                msg="SQL 执行失败",
                code=5002,
                http_status=500,
                data={"route": "text2sql", "question": question, "sql": sql},
                meta={"db_error": err, "cost_ms": cost_ms, "model_cached": model_cached}
            )

        chart = build_chart_option(columns, rows)
        t3 = time.time()

        meta_time = {
            "model_cost_ms": int((t1 - t0) * 1000),
            "sql_cost_ms": int((t2 - t1) * 1000),
            "chart_cost_ms": int((t3 - t2) * 1000),
            "cost_ms": int((t3 - t0) * 1000)
        }

        log_query(question, "text2sql", True, meta_time["cost_ms"], len(rows), sql=sql, model_cached=model_cached)

        return success_response(
            msg="查询成功" if rows else "查询成功，但没有匹配数据",
            data={
                "route": "text2sql",
                "question": question,
                "columns": columns,
                "rows": rows,
                "chart": chart
            },
            meta={"sql": sql, **meta_time, "row_count": len(rows), "model_cached": model_cached}
        )

    except requests.exceptions.RequestException as e:
        cost_ms = int((time.time() - t0) * 1000) if "t0" in locals() else 0
        log_query(question, "text2sql", False, cost_ms, 0, error=str(e))
        return error_response(
            msg="Text2SQL 模型请求失败",
            code=5003,
            http_status=500,
            data={"route": "text2sql", "question": question},
            meta={"error": str(e), "cost_ms": cost_ms}
        )
    except KeyError as e:
        cost_ms = int((time.time() - t0) * 1000) if "t0" in locals() else 0
        log_query(question, "text2sql", False, cost_ms, 0, error=f"返回结构异常: {str(e)}")
        return error_response(
            msg="Text2SQL 返回结构异常",
            code=5004,
            http_status=500,
            data={"route": "text2sql", "question": question},
            meta={"error": str(e), "cost_ms": cost_ms}
        )
    except Exception as e:
        cost_ms = int((time.time() - t0) * 1000) if "t0" in locals() else 0
        log_query(question, "text2sql", False, cost_ms, 0, error=str(e))
        return error_response(
            msg="Text2SQL 服务器内部错误",
            code=5000,
            http_status=500,
            data={"route": "text2sql", "question": question},
            meta={"error": str(e), "cost_ms": cost_ms}
        )


def handle_rag(question: str):
    try:
        t0 = time.time()
        answer = call_rag_model(question)
        t1 = time.time()

        cost_ms = int((t1 - t0) * 1000)
        log_query(question, "rag", True, cost_ms, 0)

        return success_response(
            msg="问答成功",
            data={"route": "rag", "question": question, "answer": answer},
            meta={"cost_ms": cost_ms}
        )

    except requests.exceptions.RequestException as e:
        cost_ms = int((time.time() - t0) * 1000) if "t0" in locals() else 0
        log_query(question, "rag", False, cost_ms, 0, error=str(e))
        return error_response(
            msg="RAG 模型请求失败",
            code=5101,
            http_status=500,
            data={"route": "rag", "question": question},
            meta={"error": str(e), "cost_ms": cost_ms}
        )
    except KeyError as e:
        cost_ms = int((time.time() - t0) * 1000) if "t0" in locals() else 0
        log_query(question, "rag", False, cost_ms, 0, error=f"返回结构异常: {str(e)}")
        return error_response(
            msg="RAG 返回结构异常",
            code=5102,
            http_status=500,
            data={"route": "rag", "question": question},
            meta={"error": str(e), "cost_ms": cost_ms}
        )
    except Exception as e:
        cost_ms = int((time.time() - t0) * 1000) if "t0" in locals() else 0
        log_query(question, "rag", False, cost_ms, 0, error=str(e))
        return error_response(
            msg="RAG 服务器内部错误",
            code=5100,
            http_status=500,
            data={"route": "rag", "question": question},
            meta={"error": str(e), "cost_ms": cost_ms}
        )


@app.route("/", methods=["GET"])
def home():
    return success_response(
        data={
            "service": "News Agent API",
            "ui": "/ui",
            "health": "/health",
            "docs": "/api-docs",
            "presets": "/presets"
        },
        msg="service is running"
    )


@app.route("/health", methods=["GET"])
def health():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        conn.close()

        return success_response(
            data={"app": "ok", "database": "ok", "sql_cache_size": len(_SQL_CACHE)},
            msg="healthy"
        )
    except Exception as e:
        return error_response(
            msg="health check failed",
            code=5001,
            http_status=500,
            data={"error": str(e)}
        )


@app.route("/presets", methods=["GET"])
def presets():
    items = []

    for preset_id, item in PRESET_SQL.items():
        items.append({
            "id": preset_id,
            "name": item["name"],
            "type": "sql"
        })

    for preset_id, item in PRESET_RAG.items():
        items.append({
            "id": preset_id,
            "name": item["name"],
            "type": "summary"
        })

    return success_response(data={"presets": items}, msg="获取预设问题成功")


@app.route("/preset/<preset_id>", methods=["GET"])
def preset_query(preset_id: str):
    t0 = time.time()

    if preset_id in PRESET_SQL:
        preset = PRESET_SQL[preset_id]
        question = preset["name"]
        sql = clean_sql(preset["sql"])

        is_safe, reason = is_safe_sql(sql)
        if not is_safe:
            cost_ms = int((time.time() - t0) * 1000)
            log_query(question, "preset_sql", False, cost_ms, 0, sql=sql, error=reason)
            return error_response(
                msg="预设 SQL 安全校验未通过",
                code=4201,
                http_status=400,
                data={"route": "preset_sql", "preset_id": preset_id, "sql": sql},
                meta={"reason": reason, "cost_ms": cost_ms}
            )

        columns, rows, err = execute_sql(sql)
        t1 = time.time()

        if err:
            cost_ms = int((time.time() - t0) * 1000)
            log_query(question, "preset_sql", False, cost_ms, 0, sql=sql, error=err)
            return error_response(
                msg="预设 SQL 执行失败，请检查字段名是否和数据库一致",
                code=5201,
                http_status=500,
                data={"route": "preset_sql", "preset_id": preset_id, "sql": sql},
                meta={"db_error": err, "cost_ms": cost_ms}
            )

        chart = build_chart_option(columns, rows)
        t2 = time.time()

        meta_time = {
            "model_cost_ms": 0,
            "sql_cost_ms": int((t1 - t0) * 1000),
            "chart_cost_ms": int((t2 - t1) * 1000),
            "cost_ms": int((t2 - t0) * 1000)
        }

        log_query(question, "preset_sql", True, meta_time["cost_ms"], len(rows), sql=sql)

        return success_response(
            msg="预设查询成功",
            data={
                "route": "preset_sql",
                "preset_id": preset_id,
                "question": question,
                "columns": columns,
                "rows": rows,
                "chart": chart
            },
            meta={
                "sql": sql,
                **meta_time,
                "row_count": len(rows),
                "model_cached": False
            }
        )

    if preset_id == "latest_hot_summary":
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DATE(MAX(publish_time))
            FROM news
        """)
        latest_date_row = cursor.fetchone()
        latest_date = latest_date_row[0] if latest_date_row else None

        if not latest_date:
            conn.close()
            return error_response(
                msg="没有找到新闻数据",
                code=5301,
                http_status=500
            )

        cursor.execute("""
            SELECT title
            FROM news
            WHERE DATE(publish_time) = ?
            ORDER BY id DESC
            LIMIT 10
        """, (latest_date,))

        rows = cursor.fetchall()
        conn.close()

        top_titles = [row[0] for row in rows if row and row[0]]

        answer = f"""一、热点主题
最新一天（{latest_date}）新闻主要集中在社会热点、财经动态、体育赛事、国际事件和企业新闻等方向。

二、重点新闻
""" + "\n".join(
            [f"{i+1}. {title}" for i, title in enumerate(top_titles)]
        ) + """

三、用户关注点
用户可能更关注标题明确、事件性强、与社会民生或重大热点相关的新闻内容。

四、运营建议
1. 优先推荐互动潜力较高的热点新闻。
2. 结合点击量和评论量筛选高关注内容。
3. 可以将同类新闻聚合成专题，提高用户停留时长。
"""

        cost_ms = int((time.time() - t0) * 1000)

        return success_response(
            msg="最新一天热点总结成功",
            data={
                "route": "local_summary",
                "question": "最新一天热点总结",
                "answer": answer
            },
            meta={
                "cost_ms": cost_ms,
                "latest_date": latest_date,
                "row_count": len(top_titles)
            }
        )

    if preset_id == "latest_daily_report":
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DATE(MAX(publish_time))
            FROM news
        """)
        latest_date_row = cursor.fetchone()
        latest_date = latest_date_row[0] if latest_date_row else None

        if not latest_date:
            conn.close()
            return error_response(
                msg="没有找到新闻数据",
                code=5302,
                http_status=500
            )

        cursor.execute("""
            SELECT COUNT(*)
            FROM news
            WHERE DATE(publish_time) = ?
        """, (latest_date,))
        total_news = cursor.fetchone()[0]

        cursor.execute("""
            SELECT n.title, COUNT(nc.id) AS click_count
            FROM news n
            LEFT JOIN news_clicks nc ON n.id = nc.news_id
            WHERE DATE(n.publish_time) = ?
            GROUP BY n.id, n.title
            ORDER BY click_count DESC
            LIMIT 5
        """, (latest_date,))
        top_clicks = cursor.fetchall()

        cursor.execute("""
            SELECT n.title, COUNT(c.id) AS comment_count
            FROM news n
            LEFT JOIN comments c ON n.id = c.news_id
            WHERE DATE(n.publish_time) = ?
            GROUP BY n.id, n.title
            ORDER BY comment_count DESC
            LIMIT 5
        """, (latest_date,))
        top_comments = cursor.fetchall()

        cursor.execute("""
            SELECT 
                n.title,
                COUNT(DISTINCT nc.id) + COUNT(DISTINCT c.id) AS interaction_count
            FROM news n
            LEFT JOIN news_clicks nc ON n.id = nc.news_id
            LEFT JOIN comments c ON n.id = c.news_id
            WHERE DATE(n.publish_time) = ?
            GROUP BY n.id, n.title
            ORDER BY interaction_count DESC
            LIMIT 5
        """, (latest_date,))
        top_interactions = cursor.fetchall()

        conn.close()

        answer_lines = [
            f"一、日报概览",
            f"最新一天日期：{latest_date}",
            f"新闻发布总数：{total_news}",
            "",
            "二、点击 TOP5"
        ]

        if top_clicks:
            answer_lines += [f"{i+1}. {title}（{count} 次点击）" for i, (title, count) in enumerate(top_clicks)]
        else:
            answer_lines.append("暂无点击数据")

        answer_lines.append("")
        answer_lines.append("三、评论 TOP5")

        if top_comments:
            answer_lines += [f"{i+1}. {title}（{count} 条评论）" for i, (title, count) in enumerate(top_comments)]
        else:
            answer_lines.append("暂无评论数据")

        answer_lines.append("")
        answer_lines.append("四、高互动 TOP5")

        if top_interactions:
            answer_lines += [f"{i+1}. {title}（{count} 次互动）" for i, (title, count) in enumerate(top_interactions)]
        else:
            answer_lines.append("暂无互动数据")

        answer_lines += [
            "",
            "五、运营建议",
            "1. 优先推荐点击和评论表现较好的新闻，提升首页曝光效率。",
            "2. 对高互动新闻进行专题聚合，增强用户停留和连续阅读。",
            "3. 对低点击但重要的新闻优化标题和推荐位置，提升内容分发效果。"
        ]

        cost_ms = int((time.time() - t0) * 1000)

        return success_response(
            msg="最新一天运营日报成功",
            data={
                "route": "local_summary",
                "question": "最新一天运营日报",
                "answer": "\n".join(answer_lines)
            },
            meta={
                "cost_ms": cost_ms,
                "latest_date": latest_date,
                "total_news": total_news,
                "top_click_count": len(top_clicks),
                "top_comment_count": len(top_comments),
                "top_interaction_count": len(top_interactions)
            }
        )

    return error_response(
        msg="未知预设问题",
        code=4041,
        http_status=404,
        data={"preset_id": preset_id}
    )


@app.route("/query", methods=["POST"])
def query():
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()

    if not question:
        return error_response(msg="question 不能为空", code=4001, http_status=400)

    route = detect_query_route(question)
    if route == "rag":
        return handle_rag(question)
    return handle_text2sql(question)


@app.route("/api-docs", methods=["GET"])
def api_docs_page():
    markdown = """# News Agent API 说明

这是一个统一接口的新闻智能运营助手：

- 固定运营 Dashboard：高频问题直接执行预设 SQL，更稳定、更快
- Text2SQL：统计、排行、趋势、数量分析
- RAG：新闻总结、解读、背景问答

## 接口
- GET /
- GET /health
- GET /presets
- GET /preset/<preset_id>
- POST /query
- GET /ui
"""
    return Response(markdown, content_type="text/markdown; charset=utf-8")


@app.route("/ui", methods=["GET"])
def ui():
    html = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>TX新闻智能运营助手</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 24px; background: #f6f8fb; color: #222; }
        .container { max-width: 1100px; margin: 0 auto; }
        .card { background: #fff; border-radius: 12px; padding: 18px; box-shadow: 0 2px 10px rgba(0,0,0,.06); margin-bottom: 16px; }
        h1 { margin: 0 0 8px; font-size: 24px; }
        h2 { margin: 0 0 12px; font-size: 18px; }
        .subtitle { color: #666; margin-bottom: 16px; font-size: 13px; }
        .row { display: flex; gap: 12px; align-items: center; }
        input[type=text] { flex: 1; padding: 12px; border: 1px solid #d0d7de; border-radius: 8px; font-size: 14px; }
        button { padding: 12px 18px; border: none; background: #1677ff; color: #fff; border-radius: 8px; cursor: pointer; }
        button:disabled { background: #9fbef5; cursor: not-allowed; }
        .meta { color: #666; margin-top: 10px; font-size: 13px; }
        .error { color: #c62828; white-space: pre-wrap; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; background: #fff; }
        th, td { border: 1px solid #e5e7eb; padding: 10px; text-align: left; font-size: 14px; }
        th { background: #f3f4f6; }
        #chart { width: 100%; height: 420px; margin-top: 16px; }
        .tips { color: #666; font-size: 13px; margin-top: 8px; }
        .dashboard { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 12px; }
        .preset-card {
            background: #f8fbff;
            border: 1px solid #dbeafe;
            border-radius: 12px;
            padding: 14px;
            cursor: pointer;
            transition: all .15s ease;
        }
        .preset-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(22, 119, 255, .12);
            border-color: #1677ff;
        }
        .preset-title { font-weight: 700; color: #1d4ed8; margin-bottom: 6px; }
        .preset-desc { color: #666; font-size: 12px; line-height: 1.5; }
        .badge {
            display: inline-block;
            font-size: 12px;
            padding: 2px 8px;
            border-radius: 999px;
            background: #eef4ff;
            color: #1d4ed8;
            margin-top: 8px;
        }
        pre { background: #0b1020; color: #dbeafe; padding: 12px; border-radius: 8px; overflow: auto; }
        .answer-box { background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px; line-height: 1.7; white-space: pre-wrap; }
        .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        .small-btn { padding: 8px 12px; font-size: 12px; background: #64748b; }
        @media (max-width: 900px) {
            .dashboard { grid-template-columns: repeat(2, 1fr); }
            .row { flex-direction: column; align-items: stretch; }
        }
        @media (max-width: 560px) {
            .dashboard { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>TX新闻智能运营助手</h1>
            <div class="subtitle">固定运营问题优先使用预设 SQL，速度更快、结果更稳定；自由输入作为高级查询能力。</div>

            <h2>运营 Dashboard</h2>
            <div class="dashboard">
                <div class="preset-card" onclick="runPreset('latest_daily_report')">
                    <div class="preset-title">最新一天运营日报</div>
                    <div class="preset-desc">汇总最新一天新闻数、点击、评论和高互动内容</div>
                    <span class="badge">本地总结</span>
                </div>

                <div class="preset-card" onclick="runPreset('publish_7days')">
                    <div class="preset-title">最近7天趋势</div>
                    <div class="preset-desc">基于数据库最新日期查看近7天发布数量变化</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('comment_top10')">
                    <div class="preset-title">评论 TOP10</div>
                    <div class="preset-desc">找出评论数最高的10条新闻</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('click_top10')">
                    <div class="preset-title">点击 TOP10</div>
                    <div class="preset-desc">找出点击量最高的10条新闻</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('category_count')">
                    <div class="preset-title">分类分布</div>
                    <div class="preset-desc">统计不同分类下的新闻数量</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('source_count')">
                    <div class="preset-title">来源分布</div>
                    <div class="preset-desc">统计不同来源的新闻数量</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('low_click_news')">
                    <div class="preset-title">低表现新闻</div>
                    <div class="preset-desc">查看点击量较低的新闻，辅助运营优化</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('high_interaction_news')">
                    <div class="preset-title">高互动新闻</div>
                    <div class="preset-desc">按照点击量和评论数综合排序</div>
                    <span class="badge">预设 SQL</span>
                </div>

                <div class="preset-card" onclick="runPreset('latest_hot_summary')">
                    <div class="preset-title">最新一天热点总结</div>
                    <div class="preset-desc">基于数据库最新一天新闻生成运营热点总结</div>
                    <span class="badge">本地总结</span>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="section-title">
                <h2>高级自然语言查询</h2>
                <button class="small-btn" onclick="clearAll()">清空结果</button>
            </div>
            <div class="row">
                <input id="question" type="text" placeholder="例如：统计最近7天每天的新闻发布数量 / 帮我总结一下最新一天的热点新闻" />
                <button id="submitBtn" onclick="runQuery()">查询</button>
            </div>
            <div class="tips">自动路由：统计分析走 Text2SQL，新闻总结问答走 RAG。</div>
        </div>

        <div class="card">
            <div><strong>返回状态</strong></div>
            <div id="status" class="meta">等待查询</div>
            <div id="error" class="error"></div>
        </div>

        <div class="card">
            <div><strong>结果内容</strong></div>
            <div id="tableWrap"></div>
        </div>

        <div class="card">
            <div><strong>结果图表</strong></div>
            <div id="chart"></div>
        </div>

        <div class="card">
            <div><strong>原始返回 JSON</strong></div>
            <pre id="rawJson">{}</pre>
        </div>
    </div>

    <script>
        let chart = echarts.init(document.getElementById('chart'));

        function setLoading(text) {
            document.getElementById('status').textContent = text || '查询中...';
            document.getElementById('error').textContent = '';
            document.getElementById('rawJson').textContent = '{}';
            document.getElementById('tableWrap').innerHTML = '';
            chart.clear();
        }

        function clearAll() {
            document.getElementById('question').value = '';
            document.getElementById('status').textContent = '等待查询';
            document.getElementById('error').textContent = '';
            document.getElementById('rawJson').textContent = '{}';
            document.getElementById('tableWrap').innerHTML = '';
            chart.clear();
        }

        function renderTable(columns, rows) {
            const wrap = document.getElementById('tableWrap');
            if (!columns || columns.length === 0) {
                wrap.innerHTML = '<div class="meta">暂无表头</div>';
                return;
            }

            let html = '<table><thead><tr>';
            for (const col of columns) {
                html += `<th>${escapeHtml(String(col))}</th>`;
            }
            html += '</tr></thead><tbody>';

            if (!rows || rows.length === 0) {
                html += `<tr><td colspan="${columns.length}">没有匹配数据</td></tr>`;
            } else {
                for (const row of rows) {
                    html += '<tr>';
                    for (const cell of row) {
                        html += `<td>${escapeHtml(String(cell))}</td>`;
                    }
                    html += '</tr>';
                }
            }

            html += '</tbody></table>';
            wrap.innerHTML = html;
        }

        function renderAnswer(answer) {
            const wrap = document.getElementById('tableWrap');
            wrap.innerHTML = `<div class="answer-box">${escapeHtml(answer || '')}</div>`;
        }

        function escapeHtml(str) {
            return str
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#39;');
        }

        function renderSuccess(data) {
            const statusEl = document.getElementById('status');
            const rawJsonEl = document.getElementById('rawJson');

            rawJsonEl.textContent = JSON.stringify(data, null, 2);

            if (data.code !== 0) {
                statusEl.textContent = `失败：${data.msg}`;
                document.getElementById('error').textContent = JSON.stringify(data.meta || {}, null, 2);
                return;
            }

            const result = data.data || {};
            const meta = data.meta || {};

            if (result.answer !== undefined) {
                renderAnswer(result.answer || '');
                chart.clear();
                const routeName = result.route === 'local_summary' ? '本地总结' : 'RAG问答';
                statusEl.textContent = `成功，${routeName}，总耗时 ${meta.cost_ms || 0} ms`;
                return;
            }

            renderTable(result.columns || [], result.rows || []);

            const routeName = result.route === 'preset_sql' ? '预设SQL' : 'Text2SQL';

            statusEl.textContent =
                `成功，${routeName}；总耗时 ${meta.cost_ms || 0} ms；模型 ${meta.model_cost_ms || 0} ms；SQL ${meta.sql_cost_ms || 0} ms；图表 ${meta.chart_cost_ms || 0} ms；返回 ${meta.row_count || 0} 行；模型缓存命中：${meta.model_cached ? '是' : '否'}`;

            if (result.chart && result.chart.option) {
                chart.setOption(result.chart.option, true);
            } else {
                chart.clear();
            }
        }

        async function runPreset(presetId) {
            const btn = document.getElementById('submitBtn');
            btn.disabled = true;
            setLoading('预设问题查询中...');

            try {
                const resp = await fetch(`/preset/${presetId}`);
                const data = await resp.json();
                renderSuccess(data);
            } catch (err) {
                document.getElementById('status').textContent = '请求失败';
                document.getElementById('error').textContent = String(err);
            } finally {
                btn.disabled = false;
            }
        }

        async function runQuery() {
            const btn = document.getElementById('submitBtn');
            const question = document.getElementById('question').value.trim();

            if (!question) {
                document.getElementById('error').textContent = '请输入问题';
                return;
            }

            btn.disabled = true;
            setLoading('自然语言查询中...');

            try {
                const resp = await fetch('/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ question })
                });

                const data = await resp.json();
                renderSuccess(data);
            } catch (err) {
                document.getElementById('status').textContent = '请求失败';
                document.getElementById('error').textContent = String(err);
            } finally {
                btn.disabled = false;
            }
        }

        window.addEventListener('resize', () => chart.resize());
    </script>
</body>
</html>
"""
    return Response(html, content_type="text/html; charset=utf-8")


if __name__ == "__main__":
    app.run(debug=True, port=DEFAULT_PORT)
