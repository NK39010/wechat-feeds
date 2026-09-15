"""Push one WeChat article to the reader right away, bypassing the rate-limited account list API.

    python add_article.py https://mp.weixin.qq.com/s/xxxxxxxx [更多链接 ...]

We-MP-RSS opens the article page itself and files it under its "精选文章" feed; this script waits for that, then runs
sync.py so the article is published to feeds/MP_WXS_FEATURED_ARTICLES/feed.xml.
"""

import subprocess
import sys
import time
import tomllib

import httpx

from sync import CONFIG_PATH, ROOT, SECRETS_PATH


def main() -> int:
    urls = [u for u in sys.argv[1:] if u.strip()]
    if not urls or any("mp.weixin.qq.com/s" not in u for u in urls):
        print(__doc__)
        return 2

    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    secrets_cfg = tomllib.loads(SECRETS_PATH.read_text(encoding="utf-8-sig")) if SECRETS_PATH.exists() else {}
    ak, sk = secrets_cfg.get("werss_ak", ""), secrets_cfg.get("werss_sk", "")
    if not (ak and sk):
        print("secrets.toml 里没有 Access Key")
        return 1
    werss = cfg["source"]["werss_url"].rstrip("/")
    http = httpx.Client(timeout=60, headers={"Authorization": f"AK-SK {ak}:{sk}"})

    ok = 0
    for url in urls:
        print(f"添加：{url}")
        resp = http.post(f"{werss}/api/v1/wx/mps/featured/article", json={"url": url})
        if resp.status_code in (401, 403):
            print("  ! Access Key 无效")
            return 1
        body = resp.json()
        task_id = (body.get("data") or {}).get("task_id")
        if body.get("code") != 0 or not task_id:
            print(f"  ! 提交失败：{body.get('message') or body}")
            continue
        for _ in range(60):  # up to ~3 minutes; the page is rendered in a headless browser
            time.sleep(3)
            task = (http.get(f"{werss}/api/v1/wx/mps/featured/article/tasks/{task_id}").json().get("data") or {})
            if task.get("status") not in ("pending", "running"):
                break
        status, message = task.get("status"), task.get("message", "")
        print(f"  {status}：{message}")
        ok += status in ("success", "done", "completed")

    if ok == 0:
        print("没有成功添加的文章，不运行同步")
        return 1
    print("\n运行同步 …")
    return subprocess.call([sys.executable, str(ROOT / "sync.py")], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
