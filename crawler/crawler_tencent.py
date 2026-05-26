import json
import time
import hashlib
import re
import html
from pathlib import Path
from typing import List, Dict, Optional, Any, Iterable, Tuple, Set
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

# 1. 配置

TARGET_TOTAL_ITEMS = 3000

# 每天最多新增并同步到 FastGPT 的新闻数量
TARGET_DAILY_NEW_ITEMS = 10

# 默认回溯过去30天
LOOKBACK_DAYS = 30

# 详情抓取
DETAIL_SLEEP_SECONDS = 1.2

# 站内发现阶段
DISCOVERY_SLEEP_SECONDS = 1.0

# 最大候选URL数
MAX_DISCOVER_URLS = 2500

# 最小正文长度
MIN_CONTENT_CHARS = 120

# 是否启用历史发现
ENABLE_SEARCH_DISCOVERY = True

# sitemap 最多抓多少个子 sitemap
MAX_SITEMAP_FILES = 120

# 每个入口页最多保留多少 URL
PER_SEED_PAGE_URL_LIMIT = 200

# 站内发现入口页
TENCENT_SEED_PAGES = [
    {
        "source": "Tencent News Home",
        "category": "综合",
        "url": "https://news.qq.com/"
    },
    {
        "source": "Tencent News China Channel",
        "category": "中国",
        "url": "https://news.qq.com/ch/china/"
    },
    {
        "source": "Tencent News World Channel",
        "category": "国际",
        "url": "https://news.qq.com/ch/world/"
    },
    {
        "source": "Tencent News Finance Channel",
        "category": "财经",
        "url": "https://news.qq.com/ch/finance/"
    },
    {
        "source": "Tencent News Tech Channel",
        "category": "科技",
        "url": "https://news.qq.com/ch/tech/"
    },
    {
        "source": "Tencent News Sports Channel",
        "category": "体育",
        "url": "https://news.qq.com/ch/sports/"
    },
    {
        "source": "Tencent News Ent Channel",
        "category": "娱乐",
        "url": "https://news.qq.com/ch/ent/"
    },
]

TENCENT_API_SOURCES = [
    {
        "type": "tencent_hot_ranking",
        "source": "Tencent Hot Ranking",
        "category": "综合",
        "url": "https://r.inews.qq.com/gw/event/hot_ranking_list?page_size=50"
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "*/*",
    "Connection": "keep-alive",
}

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data_tencent"
DB_FILE = DATA_DIR / "news_db.jsonl"
EXPORT_DIR = DATA_DIR / "export_txt"
RAW_JSON_DIR = DATA_DIR / "raw_json"
RAW_HTML_DIR = DATA_DIR / "raw_html"

SESSION = requests.Session()
SESSION.headers.update(HEADERS)



# 2. 通用函数
def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
    RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_local_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_html(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = re.sub(r"<script.*?>.*?</script>", "", raw_text, flags=re.I | re.S)
    text = re.sub(r"<style.*?>.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    return text


def normalize_title(title: str) -> str:
    return normalize_text(title)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        clean = parsed._replace(query="", fragment="")
        return urlunparse(clean)
    except Exception:
        return url.strip()


def safe_filename(text: str, max_len: int = 80) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n]+', "_", text).strip("_")
    return text[:max_len] if text else "untitled"


def make_news_id(source: str, title: str, url: str) -> str:
    normalized_url = normalize_url(url)
    raw = f"{source}|{title}|{normalized_url}"
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    source_slug = re.sub(r"[^a-zA-Z0-9]+", "_", source).strip("_").lower()
    return f"{source_slug}_{digest}"


def make_content_hash(title: str, content: str) -> str:
    raw = f"{normalize_text(title)}|{normalize_text(content)}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def unique_preserve_order(seq: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for x in seq:
        if not x:
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def save_jsonl(items: List[Dict], output_path: Path) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def load_existing_db(db_path: Path) -> Dict[str, Dict]:
    data: Dict[str, Dict] = {}
    if not db_path.exists():
        return data

    with open(db_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if "id" in item:
                    data[item["id"]] = item
            except Exception:
                continue
    return data


def sort_key_for_item(item: Dict):
    publish_time = item.get("publish_time", "") or ""
    updated_at = item.get("updated_at", "") or ""
    created_at = item.get("created_at", "") or ""
    return (publish_time, updated_at, created_at, item.get("id", ""))


def save_db(existing: Dict[str, Dict], db_path: Path) -> None:
    items = list(existing.values())
    items.sort(key=sort_key_for_item, reverse=True)
    save_jsonl(items, db_path)


def keep_latest_n(existing: Dict[str, Dict], limit: int) -> Dict[str, Dict]:
    items = list(existing.values())
    items.sort(key=sort_key_for_item, reverse=True)
    items = items[:limit]
    return {item["id"]: item for item in items}


def save_raw_json(name: str, data: Any) -> None:
    try:
        path = RAW_JSON_DIR / f"{safe_filename(name, 120)}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def save_raw_html(name: str, html_text: str) -> None:
    try:
        path = RAW_HTML_DIR / f"{safe_filename(name, 120)}.html"
        path.write_text(html_text, encoding="utf-8")
    except Exception:
        pass


def request_json(url: str, params: Optional[dict] = None, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt >= retries:
                print(f"[WARN] JSON请求失败: {url} | {e}")
                return None
            time.sleep(2)


def fetch_html(url: str, retries: int = 2) -> str:
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as e:
            if attempt >= retries:
                print(f"[WARN] HTML请求失败: {url} | {e}")
                return ""
            time.sleep(2)


def parse_tencent_article_locator(url: str) -> Tuple[str, str]:
    """
    返回 (kind, article_id)
    kind: "a" | "k" | ""
    """
    if not url:
        return "", ""

    url = normalize_url(url)

    patterns = [
        (r"https?://news\.qq\.com/rain/a/([A-Za-z0-9]+)$", "a"),
        (r"https?://view\.inews\.qq\.com/a/([A-Za-z0-9]+)$", "a"),
        (r"https?://view\.inews\.qq\.com/k/([A-Za-z0-9]+)$", "k"),
    ]
    for p, kind in patterns:
        m = re.search(p, url, flags=re.I)
        if m:
            return kind, m.group(1)

    return "", ""


def extract_article_id(url: str) -> str:
    _, article_id = parse_tencent_article_locator(url)
    return article_id


def is_tencent_article_url(url: str) -> bool:
    if not url:
        return False
    url = normalize_url(url)
    patterns = [
        r"^https?://news\.qq\.com/rain/a/[A-Za-z0-9]+$",
        r"^https?://view\.inews\.qq\.com/a/[A-Za-z0-9]+$",
        r"^https?://view\.inews\.qq\.com/k/[A-Za-z0-9]+$",
    ]
    return any(re.search(p, url, flags=re.I) for p in patterns)


def canonicalize_tencent_article_url(url: str) -> str:
    kind, article_id = parse_tencent_article_locator(url)
    if kind == "a" and article_id:
        return f"https://news.qq.com/rain/a/{article_id}"
    if kind == "k" and article_id:
        return f"https://view.inews.qq.com/k/{article_id}"
    return normalize_url(url)


def text_chinese_ratio(text: str) -> float:
    if not text:
        return 0.0
    chinese = re.findall(r"[\u4e00-\u9fff]", text)
    visible = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text)
    if not visible:
        return 0.0
    return len(chinese) / max(len(visible), 1)


def normalize_publish_time(text: str) -> str:
    if not text:
        return ""
    text = text.strip()

    patterns = [
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
        r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})",
        r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})",
        r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})",
        r"(\d{4}年\d{1,2}月\d{1,2}日)",
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{4}/\d{2}/\d{2})",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


def parse_datetime_any(text: str) -> Optional[datetime]:
    if not text:
        return None

    text = text.strip()

    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]

    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    try:
        t = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_recent_enough_by_lastmod(lastmod_text: str, days: int) -> bool:
    if not lastmod_text:
        return True

    dt = parse_datetime_any(lastmod_text)
    if dt is None:
        return True

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def is_good_content(title: str, content: str) -> bool:
    if not title or not content:
        return False
    if len(content) < MIN_CONTENT_CHARS:
        return False

    bad_signals = [
        "打开app阅读全文",
        "点击查看全文",
        "腾讯新闻客户端",
        "QQ浏览器",
        "正在打开",
        "网页无法访问",
        "广告",
    ]
    hit_bad = sum(1 for s in bad_signals if s in content)
    if hit_bad >= 2:
        return False

    nt = normalize_text(title)
    nc = normalize_text(content)
    if nt and nc and nt in nc and len(nc) < len(nt) + 40:
        return False

    if text_chinese_ratio(content) < 0.10:
        return False

    return True


# 3. 摘要生成

def split_sentences_zh(text: str) -> List[str]:
    if not text:
        return []
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    parts = re.split(r"(?<=[。！？!?])", text)
    out = []
    for p in parts:
        s = p.strip()
        if not s:
            continue
        s = re.sub(r"\.\.\.+$", "", s)
        s = re.sub(r"…+$", "", s)
        s = s.strip()
        if s:
            out.append(s)
    return out


def clean_summary_text(text: str) -> str:
    if not text:
        return ""
    text = clean_html(text)
    text = text.replace("摘要：", "").strip()
    text = re.sub(r"\.\.\.+$", "", text)
    text = re.sub(r"…+$", "", text)
    text = text.strip(" \n\t，。；;")
    return text.strip()


def make_summary_from_content(content: str, max_chars: int = 140) -> str:
    if not content:
        return ""

    sentences = split_sentences_zh(content)
    selected = []
    total = 0

    for s in sentences:
        if len(s) < 8:
            continue
        if any(x in s for x in ["责任编辑", "新华社发", "记者", "摄影"]):
            if total > 0:
                continue

        selected.append(s)
        total += len(s)
        if total >= max_chars:
            break

    if not selected:
        text = content[:max_chars]
        text = re.sub(r"\s+", " ", text).strip()
        return clean_summary_text(text)

    summary = "".join(selected)
    if len(summary) > max_chars:
        summary = summary[:max_chars]

    return clean_summary_text(summary)


def choose_best_summary(list_abstract: str, content: str) -> str:
    generated = make_summary_from_content(content, max_chars=140)
    if generated and len(generated) >= 30:
        return generated

    fallback = clean_summary_text(list_abstract)
    if fallback:
        return fallback

    return clean_summary_text(content[:120])


# 4. retrieval_text
def build_retrieval_text(item: Dict) -> str:
    parts = [
        f"标题：{item.get('title', '')}",
        f"发布时间：{item.get('publish_time', '')}",
        f"来源：{item.get('source', '')}",
        f"抓取入口：{item.get('fetch_source', '')}",
        f"分类：{item.get('category', '')}",
        f"链接：{item.get('url', '')}",
        ""
    ]

    summary = item.get("summary", "").strip()
    content = item.get("content", "").strip()

    if summary:
        parts.append("摘要：")
        parts.append(summary)
        parts.append("")

    if content:
        parts.append("正文：")
        parts.append(content)
        parts.append("")

    extra = item.get("extra", {})
    if extra:
        parts.append("附加信息：")
        for k, v in extra.items():
            if isinstance(v, (dict, list)):
                try:
                    v = json.dumps(v, ensure_ascii=False)
                except Exception:
                    v = str(v)
            parts.append(f"{k}: {v}")
        parts.append("")

    return "\n".join(parts).strip()


# 5. 正文解析

def parse_json_ld(soup: BeautifulSoup) -> Dict[str, str]:
    result = {"title": "", "publish_time": "", "source_name": ""}
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for sc in scripts:
        txt = sc.get_text(" ", strip=True)
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue

        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue

            headline = obj.get("headline") or obj.get("name") or ""
            date_published = obj.get("datePublished") or ""
            publisher = obj.get("publisher") or ""
            source_name = ""

            if isinstance(publisher, dict):
                source_name = publisher.get("name", "") or ""
            elif isinstance(publisher, str):
                source_name = publisher

            if headline and not result["title"]:
                result["title"] = clean_html(str(headline))
            if date_published and not result["publish_time"]:
                result["publish_time"] = clean_html(str(date_published))
            if source_name and not result["source_name"]:
                result["source_name"] = clean_html(str(source_name))

    return result


def extract_meta_content(soup: BeautifulSoup, attrs: List[Tuple[str, str]]) -> str:
    for attr_name, attr_value in attrs:
        tag = soup.find("meta", attrs={attr_name: attr_value})
        if tag and tag.get("content"):
            return clean_html(tag.get("content", ""))
    return ""


def clean_content_text(text: str) -> str:
    if not text:
        return ""

    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")

    lines = []
    bad_patterns = [
        r"^原标题[:：]?",
        r"^打开腾讯新闻.*",
        r"^点击查看全文.*",
        r"^微信扫一扫.*",
        r"^责任编辑[:：]?",
        r"^声明[:：]?",
        r"^返回顶部$",
        r"^分享$",
        r"^收藏$",
        r"^点赞$",
        r"^评论$",
        r"^举报$",
        r"^广告$",
    ]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) <= 1:
            continue
        if any(re.search(p, line) for p in bad_patterns):
            continue
        lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def score_container_text(text: str) -> int:
    if not text:
        return 0
    score = 0
    score += len(text)
    score += text.count("。") * 20
    score += text.count("，") * 5
    score += text.count("\n") * 3

    bad_words = [
        "相关推荐", "打开腾讯新闻", "点击下载", "广告", "登录", "举报",
        "微信扫一扫", "责任编辑", "免责声明"
    ]
    score -= sum(200 for w in bad_words if w in text)
    return score


def extract_best_content_from_soup(soup: BeautifulSoup) -> str:
    candidate_selectors = [
        "article",
        "main",
        '[class*="content"]',
        '[class*="article"]',
        '[class*="body"]',
        '[class*="detail"]',
        '[class*="text"]',
        '[id*="content"]',
        '[id*="article"]',
        '[id*="detail"]',
    ]

    candidates: List[str] = []

    for sel in candidate_selectors:
        try:
            nodes = soup.select(sel)
        except Exception:
            nodes = []
        for node in nodes:
            for bad in node.select("script, style, noscript, iframe, form"):
                bad.decompose()

            ps = node.find_all(["p"])
            if ps:
                text = "\n".join(p.get_text(" ", strip=True) for p in ps if p.get_text(" ", strip=True))
            else:
                text = node.get_text("\n", strip=True)

            text = clean_content_text(text)
            if text:
                candidates.append(text)

    all_ps = soup.find_all("p")
    if all_ps:
        p_text = "\n".join(p.get_text(" ", strip=True) for p in all_ps if p.get_text(" ", strip=True))
        p_text = clean_content_text(p_text)
        if p_text:
            candidates.append(p_text)

    if soup.body:
        body_text = clean_content_text(soup.body.get_text("\n", strip=True))
        if body_text:
            candidates.append(body_text)

    if not candidates:
        return ""

    candidates = unique_preserve_order(candidates)
    candidates.sort(key=score_container_text, reverse=True)
    return candidates[0]


def parse_article_html(html_text: str, page_url: str = "") -> Dict[str, str]:
    if not html_text:
        return {"title": "", "publish_time": "", "source_name": "", "content": ""}

    soup = BeautifulSoup(html_text, "html.parser")

    for bad in soup.select("script, style, noscript"):
        if bad.name == "script" and bad.get("type") == "application/ld+json":
            continue
        bad.decompose()

    title = ""
    publish_time = ""
    source_name = ""

    ld = parse_json_ld(soup)
    title = ld.get("title", "") or title
    publish_time = ld.get("publish_time", "") or publish_time
    source_name = ld.get("source_name", "") or source_name

    if not title:
        title = extract_meta_content(soup, [
            ("property", "og:title"),
            ("name", "og:title"),
            ("name", "twitter:title"),
        ]) or title

    if not source_name:
        source_name = extract_meta_content(soup, [
            ("name", "source"),
            ("property", "article:author"),
            ("name", "author"),
        ]) or source_name

    if not publish_time:
        publish_time = extract_meta_content(soup, [
            ("property", "article:published_time"),
            ("name", "pubtime"),
            ("name", "publishdate"),
            ("name", "date"),
        ]) or publish_time

    if not title:
        h1 = soup.find("h1")
        if h1:
            title = clean_html(h1.get_text(" ", strip=True))

    if not publish_time:
        full_text = soup.get_text("\n", strip=True)
        publish_time = normalize_publish_time(full_text)

    content = extract_best_content_from_soup(soup)

    return {
        "title": clean_html(title),
        "publish_time": normalize_publish_time(publish_time),
        "source_name": clean_html(source_name),
        "content": clean_content_text(content),
        "page_url": page_url,
    }


def fetch_article_detail(url: str, save_debug_html: bool = False) -> Dict[str, str]:
    kind, article_id = parse_tencent_article_locator(url)
    candidate_urls = []

    if kind == "a" and article_id:
        candidate_urls.append(f"https://news.qq.com/rain/a/{article_id}")
        candidate_urls.append(f"https://view.inews.qq.com/a/{article_id}")
    elif kind == "k" and article_id:
        candidate_urls.append(f"https://view.inews.qq.com/k/{article_id}")

    if url:
        candidate_urls.append(url)

    candidate_urls = unique_preserve_order(candidate_urls)

    best = {"title": "", "publish_time": "", "source_name": "", "content": "", "page_url": ""}
    best_len = 0

    for u in candidate_urls:
        if not is_tencent_article_url(u):
            continue

        html_text = fetch_html(u)
        if not html_text:
            continue

        if save_debug_html and article_id:
            save_raw_html(f"{article_id}_{safe_filename(u, 40)}", html_text)

        parsed = parse_article_html(html_text, page_url=u)
        content_len = len(parsed.get("content", ""))

        if content_len > best_len:
            best = parsed
            best_len = content_len

        if is_good_content(parsed.get("title", ""), parsed.get("content", "")):
            return parsed

        time.sleep(DETAIL_SLEEP_SECONDS)

    return best


# 6. 热榜种子
def parse_tencent_hot_ranking(source_cfg: Dict) -> List[Dict]:
    url = source_cfg["url"]
    data = request_json(url)
    if not data:
        return []

    save_raw_json(f"{source_cfg['source']}_latest", data)

    idlist = data.get("idlist") or []
    newslist = []

    if idlist and isinstance(idlist, list):
        first_block = idlist[0] if isinstance(idlist[0], dict) else {}
        newslist = first_block.get("newslist") or []

    items: List[Dict] = []

    for entry in newslist:
        if not isinstance(entry, dict):
            continue

        list_title = clean_html(str(entry.get("title", "")).strip())
        article_id = str(entry.get("id", "")).strip()
        list_abstract = clean_html(str(entry.get("abstract", "")).strip())
        read_count = entry.get("readCount", "")
        cover = entry.get("miniProShareImage", "")

        mobile_url = f"https://view.inews.qq.com/a/{article_id}" if article_id else ""
        desktop_url = f"https://news.qq.com/rain/a/{article_id}" if article_id else ""
        final_url = desktop_url or mobile_url

        if not list_title or not final_url:
            continue

        detail = fetch_article_detail(final_url)

        detail_title = clean_html(detail.get("title", ""))
        detail_content = clean_content_text(detail.get("content", ""))
        detail_publish = detail.get("publish_time", "")
        detail_source_name = clean_html(detail.get("source_name", ""))

        final_title = detail_title or list_title
        final_content = detail_content

        if not is_good_content(final_title, final_content):
            continue

        summary = choose_best_summary(list_abstract, final_content)

        item = {
            "id": make_news_id("Tencent News", final_title, final_url),
            "title": final_title,
            "normalized_title": normalize_title(final_title),
            "publish_time": detail_publish,
            "source": detail_source_name or "腾讯新闻",
            "fetch_source": source_cfg["source"],
            "category": source_cfg["category"],
            "url": canonicalize_tencent_article_url(final_url),
            "summary": summary,
            "content": final_content,
            "retrieval_text": "",
            "content_hash": "",
            "extra": {
                "article_id": article_id,
                "read_count": read_count,
                "cover": cover,
                "mobile_url": mobile_url,
                "raw_source_type": source_cfg["type"],
                "list_abstract_raw": list_abstract,
            },
            "created_at": now_iso(),
            "updated_at": now_iso()
        }

        item["retrieval_text"] = build_retrieval_text(item)
        item["content_hash"] = make_content_hash(item["title"], item["content"])
        items.append(item)

        time.sleep(DETAIL_SLEEP_SECONDS)

    return items


def parse_source(source_cfg: Dict) -> List[Dict]:
    source_type = source_cfg.get("type", "").strip()

    if source_type == "tencent_hot_ranking":
        return parse_tencent_hot_ranking(source_cfg)

    print(f"[WARN] 未支持的源类型: {source_type} | {source_cfg.get('source')}")
    return []



# 7. 历史发现（腾讯站内，不再用Bing）
def get_tencent_sitemap_index_url() -> str:
    robots_url = "https://news.qq.com/robots.txt"
    txt = fetch_html(robots_url)
    if not txt:
        return "https://news.qq.com/sitemap/index.xml"

    m = re.search(r"Sitemap:\s*(https?://[^\s]+)", txt, flags=re.I)
    if m:
        return m.group(1).strip()

    return "https://news.qq.com/sitemap/index.xml"


def parse_sitemap_xml(xml_text: str) -> List[Dict[str, str]]:
    """
    支持 sitemapindex/urlset
    返回 [{'loc': ..., 'lastmod': ...}, ...]
    """
    if not xml_text:
        return []

    xml_text = xml_text.strip()
    if not xml_text:
        return []

    items: List[Dict[str, str]] = []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items

    for elem in root.iter():
        tag = elem.tag
        if "}" in tag:
            tag = tag.split("}", 1)[1]

        if tag not in {"sitemap", "url"}:
            continue

        loc = ""
        lastmod = ""

        for child in list(elem):
            ctag = child.tag
            if "}" in ctag:
                ctag = ctag.split("}", 1)[1]

            if ctag == "loc" and child.text:
                loc = child.text.strip()
            elif ctag == "lastmod" and child.text:
                lastmod = child.text.strip()

        if loc:
            items.append({"loc": loc, "lastmod": lastmod})

    return items


def is_probably_sitemap_url(url: str) -> bool:
    if not url:
        return False
    url = url.lower()
    return ".xml" in url and "sitemap" in url


def extract_tencent_article_urls_from_html(html_text: str) -> List[str]:
    if not html_text:
        return []

    urls: List[str] = []
    soup = BeautifulSoup(html_text, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href = html.unescape(href)
        href = normalize_url(href)

        if is_tencent_article_url(href):
            urls.append(canonicalize_tencent_article_url(href))
            continue

        embedded = re.findall(
            r'https?://(?:news\.qq\.com/rain/a/[A-Za-z0-9]+|view\.inews\.qq\.com/a/[A-Za-z0-9]+|view\.inews\.qq\.com/k/[A-Za-z0-9]+)',
            href,
            flags=re.I
        )
        for u in embedded:
            if is_tencent_article_url(u):
                urls.append(canonicalize_tencent_article_url(u))

    patterns = [
        r'https?://news\.qq\.com/rain/a/[A-Za-z0-9]+',
        r'https?://view\.inews\.qq\.com/a/[A-Za-z0-9]+',
        r'https?://view\.inews\.qq\.com/k/[A-Za-z0-9]+',
    ]
    for p in patterns:
        for m in re.findall(p, html_text, flags=re.I):
            urls.append(canonicalize_tencent_article_url(m))

    return unique_preserve_order(urls)


def discover_tencent_urls_via_sitemaps(days: int = 30,
                                       max_total_urls: int = MAX_DISCOVER_URLS) -> List[str]:
    sitemap_index_url = get_tencent_sitemap_index_url()
    print(f"[INFO] Sitemap Index: {sitemap_index_url}")

    index_xml = fetch_html(sitemap_index_url)
    if not index_xml:
        print("[WARN] sitemap index 获取失败")
        return []

    index_items = parse_sitemap_xml(index_xml)
    sitemap_urls = [x["loc"] for x in index_items if is_probably_sitemap_url(x.get("loc", ""))]
    sitemap_urls = unique_preserve_order(sitemap_urls)

    if not sitemap_urls:
        print("[WARN] sitemap index 中未解析到子 sitemap")
        return []

    sitemap_urls = sitemap_urls[:MAX_SITEMAP_FILES]
    print(f"[INFO] 子 sitemap 数: {len(sitemap_urls)}")

    found_urls: List[str] = []
    seen: Set[str] = set()

    for idx, sitemap_url in enumerate(sitemap_urls, 1):
        if len(found_urls) >= max_total_urls:
            break

        xml_text = fetch_html(sitemap_url)
        if not xml_text:
            continue

        url_items = parse_sitemap_xml(xml_text)

        added_this_sitemap = 0
        for row in url_items:
            loc = row.get("loc", "").strip()
            lastmod = row.get("lastmod", "").strip()

            if not is_tencent_article_url(loc):
                continue

            if not is_recent_enough_by_lastmod(lastmod, days):
                continue

            loc = canonicalize_tencent_article_url(loc)

            if loc not in seen:
                seen.add(loc)
                found_urls.append(loc)
                added_this_sitemap += 1

            if len(found_urls) >= max_total_urls:
                break

        print(
            f"[DISCOVER-SITEMAP] {idx}/{len(sitemap_urls)} | "
            f"new={added_this_sitemap} | total={len(found_urls)} | {sitemap_url}"
        )

        time.sleep(DISCOVERY_SLEEP_SECONDS)

    return unique_preserve_order(found_urls)


def discover_tencent_urls_via_seed_pages(max_total_urls: int = MAX_DISCOVER_URLS) -> List[str]:
    found_urls: List[str] = []
    seen: Set[str] = set()

    for idx, cfg in enumerate(TENCENT_SEED_PAGES, 1):
        if len(found_urls) >= max_total_urls:
            break

        page_url = cfg["url"]
        html_text = fetch_html(page_url)
        if not html_text:
            continue

        urls = extract_tencent_article_urls_from_html(html_text)
        urls = urls[:PER_SEED_PAGE_URL_LIMIT]

        added_this_page = 0
        for u in urls:
            if u not in seen:
                seen.add(u)
                found_urls.append(u)
                added_this_page += 1

            if len(found_urls) >= max_total_urls:
                break

        print(
            f"[DISCOVER-SEED] {idx}/{len(TENCENT_SEED_PAGES)} | "
            f"new={added_this_page} | total={len(found_urls)} | {page_url}"
        )

        time.sleep(DISCOVERY_SLEEP_SECONDS)

    return unique_preserve_order(found_urls)


def discover_tencent_urls_via_search(days: int = 30,
                                     keywords: Optional[List[str]] = None,
                                     max_total_urls: int = MAX_DISCOVER_URLS) -> List[str]:
    """
    保留原函数名，避免主流程大改。
    现在不再使用 Bing，而是改成腾讯站内发现。
    """
    sitemap_urls = discover_tencent_urls_via_sitemaps(
        days=days,
        max_total_urls=max_total_urls
    )

    remaining = max_total_urls - len(sitemap_urls)
    if remaining <= 0:
        return unique_preserve_order(sitemap_urls)

    seed_urls = discover_tencent_urls_via_seed_pages(max_total_urls=remaining)

    all_urls = unique_preserve_order(sitemap_urls + seed_urls)
    return all_urls[:max_total_urls]



# 8. URL -> Item
def build_item_from_detail(url: str,
                           fetch_source: str = "Tencent Site Discovery",
                           category: str = "综合") -> Optional[Dict]:
    if not is_tencent_article_url(url):
        return None

    url = canonicalize_tencent_article_url(url)
    detail = fetch_article_detail(url)

    title = clean_html(detail.get("title", ""))
    content = clean_content_text(detail.get("content", ""))
    publish_time = detail.get("publish_time", "")
    source_name = clean_html(detail.get("source_name", "")) or "腾讯新闻"

    if not is_good_content(title, content):
        return None

    article_id = extract_article_id(url)
    summary = choose_best_summary("", content)

    item = {
        "id": make_news_id("Tencent News", title, url),
        "title": title,
        "normalized_title": normalize_title(title),
        "publish_time": publish_time,
        "source": source_name,
        "fetch_source": fetch_source,
        "category": category,
        "url": url,
        "summary": summary,
        "content": content,
        "retrieval_text": "",
        "content_hash": "",
        "extra": {
            "article_id": article_id,
            "raw_source_type": "site_discovery",
        },
        "created_at": now_iso(),
        "updated_at": now_iso()
    }

    item["retrieval_text"] = build_retrieval_text(item)
    item["content_hash"] = make_content_hash(item["title"], item["content"])
    return item


def build_items_from_urls(urls: List[str],
                          fetch_source: str = "Tencent Site Discovery",
                          category: str = "综合") -> List[Dict]:
    urls = [canonicalize_tencent_article_url(u) for u in urls if is_tencent_article_url(u)]
    urls = unique_preserve_order(urls)

    items: List[Dict] = []
    total = len(urls)

    for idx, url in enumerate(urls, 1):
        try:
            item = build_item_from_detail(url, fetch_source, category)
            if item is not None:
                items.append(item)
        except Exception as e:
            print(f"[WARN] 详情抓取失败: {url} | {e}")

        if idx % 20 == 0 or idx == total:
            print(f"[DETAIL] {idx}/{total} | 当前有效 {len(items)} 条")

        time.sleep(DETAIL_SLEEP_SECONDS)

    return items


# 9. 去重
def merge_news(existing: Dict[str, Dict], new_items: List[Dict], max_added: Optional[int] = None):
    added: List[Dict] = []
    updated: List[Dict] = []

    existing_title_keys = set()
    existing_content_hashes = set()
    existing_url_keys = set()

    for old_item in existing.values():
        source = old_item.get("source", "")
        normalized_title = old_item.get("normalized_title") or normalize_title(old_item.get("title", ""))
        content_hash = old_item.get("content_hash", "")
        url = normalize_url(old_item.get("url", ""))

        if source and normalized_title:
            existing_title_keys.add((source, normalized_title))
        if content_hash:
            existing_content_hashes.add(content_hash)
        if source and url:
            existing_url_keys.add((source, url))

    for item in new_items:
        if max_added is not None and len(added) >= max_added:
            break

        old = existing.get(item["id"])

        if old is not None:
            old_content = old.get("content", "") or ""
            new_content = item.get("content", "") or ""
            if (
                old.get("content_hash") != item.get("content_hash")
                and len(new_content) >= len(old_content)
            ):
                item["created_at"] = old.get("created_at", item["created_at"])
                item["updated_at"] = now_iso()
                existing[item["id"]] = item
                updated.append(item)
            continue

        title_key = (item["source"], item["normalized_title"])
        url_key = (item["source"], item["url"])

        if title_key in existing_title_keys:
            continue

        if url_key in existing_url_keys:
            continue

        if item["content_hash"] in existing_content_hashes:
            continue

        existing[item["id"]] = item
        added.append(item)

        existing_title_keys.add(title_key)
        existing_url_keys.add(url_key)
        existing_content_hashes.add(item["content_hash"])

    return existing, added, updated


# 10. 导出txt
def export_txt(item: Dict, output_dir: Path) -> Path:
    title_part = safe_filename(item["title"], max_len=50)
    filename = f"{item['id']}_{title_part}.txt"
    path = output_dir / filename

    content = item["retrieval_text"].strip() + "\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return path


def export_items_to_txt(items: List[Dict], output_dir: Path) -> None:
    for item in items:
        try:
            export_txt(item, output_dir)
        except Exception as e:
            print(f"[WARN] 导出TXT失败: {item.get('title', '')} | {e}")


# 11. 主流程
def main() -> None:
    ensure_dirs()
    print(f"启动时间: {now_local_str()}")
    print(f"输出目录: {DATA_DIR}")

    existing = load_existing_db(DB_FILE)
    print(f"已有数据库: {len(existing)} 条")

    total_fetched = 0
    total_added = 0
    total_updated = 0

    # 1. 热榜种子
    print("\n========== 阶段1：热榜种子抓取 ==========")
    seed_items: List[Dict] = []

    for source_cfg in TENCENT_API_SOURCES:
        try:
            print(f"开始抓取: {source_cfg['source']} | {source_cfg['url']}")
            data = parse_source(source_cfg)
            print(f"[OK] {source_cfg['source']} 抓到有效新闻 {len(data)} 条")
            seed_items.extend(data)
        except Exception as e:
            print(f"[ERROR] {source_cfg['source']} 抓取失败: {e}")

    total_fetched += len(seed_items)
    needed = TARGET_DAILY_NEW_ITEMS - total_added
    existing, added_seed, updated_seed = merge_news(existing, seed_items, max_added=needed)
    total_added += len(added_seed)
    total_updated += len(updated_seed)

    save_db(existing, DB_FILE)

    print(f"[INFO] 热榜后数据库总量: {len(existing)} 条")
    print(f"[INFO] 热榜新增: {len(added_seed)} 条")
    print(f"[INFO] 热榜更新: {len(updated_seed)} 条")

    if total_added >= TARGET_DAILY_NEW_ITEMS:
        print(f"[INFO] 今天已新增 {total_added} 条，达到每日上限 {TARGET_DAILY_NEW_ITEMS} 条，停止抓取。")

    # 2. 历史发现
    if ENABLE_SEARCH_DISCOVERY and total_added < TARGET_DAILY_NEW_ITEMS:
        print("\n========== 阶段2：腾讯站内历史发现抓取（慢速） ==========")

        discovered_urls = discover_tencent_urls_via_search(
            days=LOOKBACK_DAYS,
            keywords=None,
            max_total_urls=MAX_DISCOVER_URLS
        )

        print(f"[INFO] 历史发现候选链接: {len(discovered_urls)} 个")

        if discovered_urls:
            search_items = build_items_from_urls(
                discovered_urls,
                fetch_source=f"Tencent Site Discovery {LOOKBACK_DAYS}d",
                category="综合"
            )

            print(f"[INFO] 历史发现转有效正文新闻: {len(search_items)} 条")
            total_fetched += len(search_items)

            needed = TARGET_DAILY_NEW_ITEMS - total_added
            existing, added_search, updated_search = merge_news(existing, search_items, max_added=needed)

            total_added += len(added_search)
            total_updated += len(updated_search)

            save_db(existing, DB_FILE)

            print(f"[INFO] 历史发现新增: {len(added_search)} 条")
            print(f"[INFO] 历史发现更新: {len(updated_search)} 条")
            print(f"[INFO] 历史发现后数据库总量: {len(existing)} 条")

    print("\n" + "=" * 70)
    print("抓取完成")
    print(f"每日新增上限：{TARGET_DAILY_NEW_ITEMS}")
    print(f"历史回溯天数：{LOOKBACK_DAYS}")
    print(f"本次有效抓取总数：{total_fetched} 条")
    print(f"本次新增总数：{total_added} 条")
    print(f"本次更新总数：{total_updated} 条")
    print(f"数据库当前总量：{len(existing)} 条")
    print(f"主数据库：{DB_FILE}")
    print(f"原始JSON目录：{RAW_JSON_DIR}")
    print(f"原始HTML目录：{RAW_HTML_DIR}")

    print("=" * 70)


if __name__ == "__main__":
    main()