import os
import random
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "news.db")


CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "西安", "重庆"]
GENDERS = ["男", "女"]
DEVICES = ["iOS", "Android", "Web"]
CHANNELS = ["首页", "推荐", "搜索", "科技", "体育", "财经", "娱乐", "社会"]
SENTIMENTS = ["正向", "中立", "负向"]

COMMENT_TEMPLATES = [
    "这条新闻很有意思",
    "内容写得不错，信息量很大",
    "这个话题值得继续关注",
    "看完之后很有启发",
    "感觉这个报道角度很好",
    "说得比较客观",
    "这个事情后续会怎么发展",
    "支持一下这篇报道",
    "感觉一般，还想看更多细节",
    "这个新闻挺重要的"
]


def random_datetime(start_dt: datetime, end_dt: datetime) -> str:
    delta = end_dt - start_dt
    seconds = int(delta.total_seconds())
    random_seconds = random.randint(0, seconds)
    dt = start_dt + timedelta(seconds=random_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_news_ids(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT id, category FROM news")
    return cursor.fetchall()


def insert_users(conn, user_count=50):
    cursor = conn.cursor()

    users = []
    start_dt = datetime(2026, 1, 1, 0, 0, 0)
    end_dt = datetime(2026, 4, 1, 23, 59, 59)

    for i in range(1, user_count + 1):
        username = f"user_{i:03d}"
        gender = random.choice(GENDERS)
        age = random.randint(18, 60)
        city = random.choice(CITIES)
        register_time = random_datetime(start_dt, end_dt)
        device_type = random.choice(DEVICES)

        users.append((username, gender, age, city, register_time, device_type))

    cursor.executemany("""
        INSERT INTO users (username, gender, age, city, register_time, device_type)
        VALUES (?, ?, ?, ?, ?, ?)
    """, users)

    conn.commit()
    print(f"已插入 users 数据 {user_count} 条")


def get_user_ids(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT id, device_type FROM users")
    return cursor.fetchall()


def insert_news_clicks(conn, click_count=2000):
    cursor = conn.cursor()

    user_rows = get_user_ids(conn)
    news_rows = get_news_ids(conn)

    if not user_rows:
        raise ValueError("users 表没有数据，请先插入 users 数据")
    if not news_rows:
        raise ValueError("news 表没有数据，请先确认 news 表已有新闻数据")

    start_dt = datetime(2026, 3, 28, 0, 0, 0)
    end_dt = datetime(2026, 4, 1, 23, 59, 59)

    click_rows = []

    for _ in range(click_count):
        user_id, user_device = random.choice(user_rows)
        news_id, _category = random.choice(news_rows)

        click_time = random_datetime(start_dt, end_dt)
        stay_seconds = random.randint(5, 300)
        channel = random.choice(CHANNELS)

        # 设备类型大多数跟用户设备一致，也允许少量随机
        device_type = user_device if random.random() < 0.8 else random.choice(DEVICES)

        click_rows.append((
            user_id,
            news_id,
            click_time,
            stay_seconds,
            channel,
            device_type
        ))

    cursor.executemany("""
        INSERT INTO news_clicks (user_id, news_id, click_time, stay_seconds, channel, device_type)
        VALUES (?, ?, ?, ?, ?, ?)
    """, click_rows)

    conn.commit()
    print(f"已插入 news_clicks 数据 {click_count} 条")


def insert_comments(conn, comment_count=300):
    cursor = conn.cursor()

    user_rows = get_user_ids(conn)
    news_rows = get_news_ids(conn)

    if not user_rows:
        raise ValueError("users 表没有数据，请先插入 users 数据")
    if not news_rows:
        raise ValueError("news 表没有数据，请先确认 news 表已有新闻数据")

    start_dt = datetime(2026, 3, 28, 0, 0, 0)
    end_dt = datetime(2026, 4, 1, 23, 59, 59)

    comment_rows = []

    for _ in range(comment_count):
        user_id, _user_device = random.choice(user_rows)
        news_id, _category = random.choice(news_rows)

        comment_text = random.choice(COMMENT_TEMPLATES)
        comment_time = random_datetime(start_dt, end_dt)
        sentiment = random.choice(SENTIMENTS)
        likes_count = random.randint(0, 100)

        comment_rows.append((
            user_id,
            news_id,
            comment_text,
            comment_time,
            sentiment,
            likes_count
        ))

    cursor.executemany("""
        INSERT INTO comments (user_id, news_id, comment_text, comment_time, sentiment, likes_count)
        VALUES (?, ?, ?, ?, ?, ?)
    """, comment_rows)

    conn.commit()
    print(f"已插入 comments 数据 {comment_count} 条")


def main():
    conn = sqlite3.connect(DB_PATH)

    try:
        insert_users(conn, user_count=50)
        insert_news_clicks(conn, click_count=2000)
        insert_comments(conn, comment_count=300)
        print("模拟数据插入完成。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()