import subprocess
import sys
import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

PROJECT_DIR = Path(os.getenv("PROJECT_DIR", Path(__file__).resolve().parent))

CRAWLER_SCRIPT = PROJECT_DIR / "crawler" / "crawler_tencent.py"

IMPORT_SCRIPT = PROJECT_DIR / "import_news_jsonl_to_sqlite.py"
SYNC_SCRIPT = PROJECT_DIR / "sync_fastgpt_kb.py"


def run_step(name, cmd):
    print(f"\n========== {name} ==========")
    result = subprocess.run(cmd, cwd=PROJECT_DIR)

    if result.returncode != 0:
        raise RuntimeError(f"{name} 执行失败")


def main():
    run_step("1. 运行腾讯新闻爬虫", [sys.executable, str(CRAWLER_SCRIPT)])
    run_step("2. 导入 news_db.jsonl 到 SQLite", [sys.executable, str(IMPORT_SCRIPT)])
    run_step("3. 同步新增新闻到 FastGPT 知识库", [sys.executable, str(SYNC_SCRIPT)])
    print("\n每日更新完成。")


if __name__ == "__main__":
    main()