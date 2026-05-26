import json
import os
import re
import time
import requests
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Dict, List, Any
from dotenv import load_dotenv

load_dotenv()

FASTGPT_BASE_URL = os.getenv("FASTGPT_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
API_URL = f"{FASTGPT_BASE_URL}/api/v1/chat/completions"

API_KEY = os.getenv("RAG_API_KEY")
APP_ID = os.getenv("RAG_APP_ID")

INPUT_FILE = "eval/test.jsonl"
OUTPUT_FILE = "eval/results.jsonl"

DEBUG = True
SLEEP_SECONDS = 0.2

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}


REFUSE_PATTERNS = [
    "无法确定", "无法判断", "无法确认",
    "尚不清楚", "没有足够信息", "信息不足",
    "正文信息不足"
]

BAD_PATTERNS = [
    "截至2023年",
    "建议查看",
    "权威媒体",
    "搜索引擎"
]


def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = str(text).lower()
    text = text.replace("：", ":").replace("；", ";").replace("，", ",")
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("％", "%")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("　", " ")
    text = text.replace("－", "-").replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", "", text)
    return text


def contains_any(text: str, patterns: List[str]) -> bool:
    norm_text = normalize_text(text)
    return any(normalize_text(p) in norm_text for p in patterns)


def fuzzy_ratio(a: str, b: str) -> float:
    a = normalize_text(a)
    b = normalize_text(b)

    if not a or not b:
        return 0.0

    return SequenceMatcher(None, a, b).ratio()


def is_pure_refusal(answer: str) -> bool:
    """
    只把“纯拒答”判为拒答。
    避免这种情况被误杀：
    “核心结论：会议审议通过了xxx。其他信息无法确定。”
    """
    norm = normalize_text(answer)

    if not norm:
        return True

    has_refusal = any(normalize_text(x) in norm for x in REFUSE_PATTERNS)

    return has_refusal and len(norm) < 120


def extract_numbers(text: str) -> List[str]:
    """
    提取数字表达：
    - 66-81
    - 3:0
    - 180万元
    - 4.72亿元
    - 19.1%
    - 2026年4月1日会拆成 2026年 / 4月 / 1日
    """
    if not text:
        return []

    pattern = r"""
        \d+(?:\.\d+)?\s*[-:：比]\s*\d+ |
        \d+(?:\.\d+)?(?:%|万元|亿元|万港元|万美元|万台|万辆|万股|元/㎡|元|件|套|个|处|场|人|分|米|小时|分钟|年|月|日|辆|亩|次|家|倍) |
        \d+(?:\.\d+)?
    """

    nums = re.findall(pattern, text, flags=re.VERBOSE)

    seen = set()
    out = []

    for n in nums:
        n = normalize_text(n)
        if n and n not in seen:
            seen.add(n)
            out.append(n)

    return out


def number_match(ref_num: str, answer: str) -> bool:
    """
    数字匹配放宽一点：
    - 4.72亿元 vs 4.7亿元 不算完全等价，但如果答案含 4.7，也可以认为部分命中
    - 2026年4月1日拆分后也能命中
    """
    ref = normalize_text(ref_num)
    ans = normalize_text(answer)

    if not ref or not ans:
        return False

    if ref in ans:
        return True

    raw_ref_digits = re.sub(r"[^\d.]", "", ref)
    if raw_ref_digits and raw_ref_digits in ans:
        return True

    return False


def split_reference_segments(reference_answer: str) -> List[str]:
    """
    如果 test.jsonl 没有 reference_segments，就从 reference_answer 里粗略切分。
    """
    if not reference_answer:
        return []

    parts = re.split(r"[；;。.\n]", reference_answer)
    return [p.strip() for p in parts if p.strip()]


def call_api(messages: List[Dict[str, str]]) -> str:
    payload = {
        "appId": APP_ID,
        "stream": False,
        "detail": True,
        "max_tokens": 512,
        "messages": messages
    }

    resp = requests.post(API_URL, headers=HEADERS, json=payload, timeout=60)

    if DEBUG:
        print("\n[DEBUG] status:", resp.status_code)
        print("[DEBUG] raw:", resp.text[:1200])

    resp.raise_for_status()
    data = resp.json()

    if "choices" in data and data["choices"]:
        content = data["choices"][0]["message"].get("content", "")
        if content and content.strip():
            return content.strip()

    if "responseData" in data:
        for node in reversed(data["responseData"]):
            if node.get("moduleType") == "chatNode":
                for key in ["text", "content", "answerText", "response", "value"]:
                    v = node.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()

                for history_key in ["historyPreview", "history"]:
                    history = node.get(history_key, [])
                    for h in reversed(history):
                        if h.get("obj") == "AI":
                            v = h.get("value", "")
                            if isinstance(v, str) and v.strip():
                                return v.strip()

    for key in ["text", "content", "message", "response"]:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()

    return ""


def hit_alias_group(answer: str, alias_group: Any) -> bool:
    norm_answer = normalize_text(answer)

    if isinstance(alias_group, str):
        alias = normalize_text(alias_group)
        return alias in norm_answer if alias else False

    if isinstance(alias_group, list):
        return any(
            normalize_text(x) in norm_answer
            for x in alias_group
            if isinstance(x, str) and normalize_text(x)
        )

    return False


def score_answer(
    answer: str,
    reference_answer: str,
    question_type: str,
    eval_aliases: List[Any],
    min_alias_hits: int,
    require_number_if_present: bool,
    reference_segments: List[str] = None
) -> Dict[str, Any]:

    if reference_segments is None:
        reference_segments = []

    if not reference_segments:
        reference_segments = split_reference_segments(reference_answer)

    has_bad_pattern = contains_any(answer, BAD_PATTERNS)
    has_refuse_pattern = is_pure_refusal(answer)

    matched_aliases = []
    for alias_group in eval_aliases:
        if hit_alias_group(answer, alias_group):
            matched_aliases.append(alias_group)

    ref_numbers = extract_numbers(reference_answer)
    matched_numbers = [n for n in ref_numbers if number_match(n, answer)]

    matched_segments = []
    norm_answer = normalize_text(answer)

    for seg in reference_segments:
        norm_seg = normalize_text(seg)
        if not norm_seg:
            continue

        if norm_seg in norm_answer:
            matched_segments.append(seg)
        elif fuzzy_ratio(seg, answer) >= 0.48:
            matched_segments.append(seg)

    alias_hit_count = len(matched_aliases)
    number_hit_count = len(matched_numbers)
    segment_hit_count = len(matched_segments)
    similarity = fuzzy_ratio(answer, reference_answer)

    if question_type == "detail":
        passed = (
            number_hit_count >= 1
            or alias_hit_count >= min_alias_hits
            or segment_hit_count >= 1
            or similarity >= 0.45
        )

    elif question_type in ["direct_title", "paraphrase", "coreference"]:
        passed = (
            alias_hit_count >= min_alias_hits
            or segment_hit_count >= 1
            or number_hit_count >= 1
            or similarity >= 0.42
        )

    elif question_type == "multi_news_classification":
        passed = (
            alias_hit_count >= min_alias_hits
            or segment_hit_count >= 1
        )

    else:
        passed = (
            alias_hit_count >= min_alias_hits
            or segment_hit_count >= 1
            or number_hit_count >= 1
        )

    if has_bad_pattern or has_refuse_pattern or not answer.strip():
        passed = False

    return {
        "passed": passed,
        "score": alias_hit_count,
        "matched_aliases": matched_aliases,
        "reference_numbers": ref_numbers,
        "matched_numbers": matched_numbers,
        "reference_segments": reference_segments,
        "matched_segments": matched_segments,
        "similarity": similarity,
        "has_bad_pattern": has_bad_pattern,
        "has_refuse_pattern": has_refuse_pattern
    }


def build_question_for_api(item: Dict[str, Any]) -> str:
    question = item["question"]
    news_id = item.get("news_id")
    question_type = item.get("type", "")

    extra = (
        "回答要求："
        "只根据命中的新闻内容回答。"
        "优先保留原文中的关键数字、日期、金额、百分比、比分、对象和结果。"
        "不要泛泛概括。"
        "如果标题、摘要或正文任一部分足以回答，就直接回答，不要过度拒答。"
    )

    if question_type == "detail":
        extra += (
            "这是细节题。"
            "请直接给出最关键的具体数字或事实。"
            "如果有多个关键数字，可以列出1到3个。"
        )

    elif question_type == "coreference":
        extra += (
            "这是对该新闻的追问。"
            "不要当成全库问题。"
            "优先回答这篇新闻中的关键结果或结论。"
        )

    elif question_type in ["direct_title", "paraphrase"]:
        extra += (
            "这是概括题。"
            "请用1到3个核心事实概括，不要扩展背景分析。"
        )

    elif question_type == "multi_news_classification":
        extra += (
            "这是多新闻分类题。"
            "只有当前命中结果足以代表问题范围时才回答。"
        )

    if news_id:
        return (
            f"新闻ID：{news_id}\n"
            f"问题：{question}\n"
            f"{extra}"
        )

    return (
        f"问题：{question}\n"
        f"{extra}"
    )


def main():
    results = []
    total = 0
    passed_count = 0
    type_stats = defaultdict(lambda: {"total": 0, "passed": 0})

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        items = [json.loads(line) for line in f]

    items = [x for x in items if x.get("enabled", True)]

    for item in items:
        qid = item["id"]
        question = item["question"]

        reference_answer = (
            item.get("reference_answer")
            or item.get("answer", "")
        )

        reference_segments = item.get("reference_segments", [])

        question_type = item.get("type", "unknown")
        news_id = item.get("news_id")
        eval_aliases = item.get("eval_aliases", [])
        min_alias_hits = item.get("min_alias_hits", 1)
        require_number_if_present = item.get("require_number_if_present", False)

        print(f"\n========== CASE START: {qid} ==========")

        question_for_api = build_question_for_api(item)

        messages = [
            {
                "role": "user",
                "content": question_for_api
            }
        ]

        try:
            answer = call_api(messages)

            print("\n==============================")
            print(f"[QID] {qid}")
            print(f"[TYPE] {question_type}")
            print(f"[NEWS_ID] {news_id}")
            print(f"[QUESTION]\n{question}")
            print(f"[API_QUESTION]\n{question_for_api}")
            print(f"[REFERENCE]\n{reference_answer}")
            print(f"[ANSWER]\n{answer}")
            print("==============================\n")

            score_info = score_answer(
                answer=answer,
                reference_answer=reference_answer,
                question_type=question_type,
                eval_aliases=eval_aliases,
                min_alias_hits=min_alias_hits,
                require_number_if_present=require_number_if_present,
                reference_segments=reference_segments
            )

            if DEBUG:
                print("[DEBUG] eval_aliases:", eval_aliases)
                print("[DEBUG] matched_aliases:", score_info["matched_aliases"])
                print("[DEBUG] reference_numbers:", score_info["reference_numbers"])
                print("[DEBUG] matched_numbers:", score_info["matched_numbers"])
                print("[DEBUG] reference_segments:", score_info["reference_segments"])
                print("[DEBUG] matched_segments:", score_info["matched_segments"])
                print("[DEBUG] similarity:", round(score_info["similarity"], 4))
                print("[DEBUG] has_bad_pattern:", score_info["has_bad_pattern"])
                print("[DEBUG] has_refuse_pattern:", score_info["has_refuse_pattern"])

            record = {
                "id": qid,
                "news_id": news_id,
                "type": question_type,
                "question": question,
                "api_question": question_for_api,
                "reference_answer": reference_answer,
                "answer": answer,
                "eval_aliases": eval_aliases,
                "matched_aliases": score_info["matched_aliases"],
                "reference_numbers": score_info["reference_numbers"],
                "matched_numbers": score_info["matched_numbers"],
                "reference_segments": score_info["reference_segments"],
                "matched_segments": score_info["matched_segments"],
                "similarity": score_info["similarity"],
                "score": score_info["score"],
                "passed": score_info["passed"]
            }

        except Exception as e:
            record = {
                "id": qid,
                "news_id": news_id,
                "type": question_type,
                "question": question,
                "reference_answer": reference_answer,
                "answer": "",
                "passed": False,
                "error": str(e)
            }

        results.append(record)

        total += 1
        type_stats[question_type]["total"] += 1

        if record["passed"]:
            passed_count += 1
            type_stats[question_type]["passed"] += 1

        print(f"[{qid}] passed={record['passed']}")
        time.sleep(SLEEP_SECONDS)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("\n========== RESULT ==========")
    print(f"{passed_count}/{total} = {passed_count / total:.2%}" if total else "0/0 = 0.00%")

    print("\n========== BY TYPE ==========")
    for t, s in type_stats.items():
        acc = s["passed"] / s["total"] if s["total"] else 0
        print(f"{t}: {s['passed']}/{s['total']} = {acc:.2%}")

    print(f"\n结果已写入：{OUTPUT_FILE}")


if __name__ == "__main__":
    main()