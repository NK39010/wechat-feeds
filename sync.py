"""Sync WeChat articles from a local We-MP-RSS into per-account RSS feeds for an e-ink reader.

    python sync.py              # sync + commit + push (per config.toml)
    python sync.py --no-push    # sync only, leave git alone

Output lives under docs/<secret_path>/ :
    index.html            all feeds with their subscribe URLs
    feeds.opml            the same list for readers that import OPML
    <feed_id>/feed.xml    one RSS feed per account
    <feed_id>/<id>.html   one page per article (RSS item links point here, readers that follow links get the text)
    img/<hash>.jpg        re-hosted images (WeChat's image CDN refuses hotlinking)
"""

import argparse
import hashlib
import io
import json
import re
import secrets
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import httpx
from PIL import Image, ImageFilter

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.toml"
SECRETS_PATH = ROOT / "secrets.toml"
STATE_PATH = ROOT / "data" / "state.json"
REFRESH_STATE_PATH = ROOT / "data" / "refresh_state.json"
DOCS = ROOT / "docs"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
IMG_HEADERS = {
    "Referer": "https://mp.weixin.qq.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
}

PAGE_CSS = """
  body{margin:0;background:#fff;color:#1c1c1a;font:17px/1.8 -apple-system,"Segoe UI","Microsoft YaHei",serif}
  main{max-width:40em;margin:0 auto;padding:28px 18px 60px}
  h1{font-size:1.45em;line-height:1.4;margin:0 0 .4em}
  .meta{color:#6b6b66;font-size:.85em;margin:0 0 2em}
  p,div{margin:0 0 .9em}
  img{max-width:100%;height:auto;display:block;margin:1em auto}
  blockquote{margin:1.2em 0;padding:.6em 1em;border-left:3px solid #999;color:#444}
  table{border-collapse:collapse;max-width:100%}td,th{border:1px solid #ccc;padding:4px 6px}
  a{color:#1c1c1a}
"""


# ---------------------------------------------------------------- HTML cleaning

class Cleaner(HTMLParser):
    """Rebuild WeChat article HTML with a small tag whitelist and no inline styles.

    WeChat markup hides the body (visibility:hidden), lazy-loads images via data-src and nests dozens of styled
    <section>s; e-ink readers render that poorly, so keep only structure, text, links and images.
    """

    KEEP = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "em", "i", "u", "blockquote",
            "ul", "ol", "li", "a", "img", "figure", "figcaption", "table", "thead", "tbody", "tr", "td", "th",
            "pre", "code", "hr", "sup", "sub"}
    AS_DIV = {"section", "div", "article"}
    DROP = {"script", "style", "iframe", "svg", "noscript", "button", "input", "form", "video", "audio",
            "mpvoice", "mpvideo", "mp-common-profile", "mp-common-videosnap", "mp-miniprogram", "qqmusic"}
    VOID = {"br", "img", "hr"}

    def __init__(self, image_url_for):
        super().__init__(convert_charrefs=True)
        self.image_url_for = image_url_for  # callable: original src -> re-hosted URL or None
        self.out: list[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.drop_depth or tag in self.DROP:
            if tag not in self.VOID:
                self.drop_depth += 1
            return
        a = dict(attrs)
        if tag == "img":
            src = a.get("data-src") or a.get("src") or ""
            if src.startswith("//"):
                src = "https:" + src
            new = self.image_url_for(src) if src.startswith("http") else None
            if new:
                self.out.append(f'<img src="{escape(new)}" alt=""/>')
        elif tag == "a":
            href = a.get("href", "")
            self.out.append(f'<a href="{escape(href)}">' if href.startswith("http") else "<a>")
        elif tag in self.KEEP:
            self.out.append(f"<{tag}/>" if tag in self.VOID else f"<{tag}>")
        elif tag in self.AS_DIV:
            self.out.append("<div>")

    def handle_endtag(self, tag):
        if self.drop_depth:
            if tag in self.DROP or tag not in self.VOID:
                self.drop_depth -= 1
            return
        if tag in self.KEEP and tag not in self.VOID:
            self.out.append(f"</{tag}>")
        elif tag in self.AS_DIV:
            self.out.append("</div>")

    def handle_data(self, data):
        if not self.drop_depth:
            self.out.append(escape(data))

    def html(self) -> str:
        text = "".join(self.out)
        # collapse the empty wrappers WeChat leaves behind
        for _ in range(4):
            text = re.sub(r"<(div|p|span)>\s*(<br/>\s*)*</\1>", "", text)
        return text.strip()


# ---------------------------------------------------------------- images

class ImageStore:
    def __init__(self, out_dir: Path, base_url: str, cfg: dict):
        self.out_dir = out_dir
        self.base_url = base_url
        self.cfg = cfg
        self.http = httpx.Client(timeout=60, follow_redirects=True, headers=IMG_HEADERS)
        self.used: set[str] = set()

    def url_for(self, src: str) -> str | None:
        name = hashlib.sha1(src.encode()).hexdigest()[:20] + ".jpg"
        path = self.out_dir / name
        if not path.exists():
            try:
                resp = self.http.get(src)
                resp.raise_for_status()
                if not self._convert(resp.content, path):
                    return None
            except (httpx.HTTPError, OSError) as e:
                print(f"    ! 图片下载失败 {src[:80]}: {e}")
                return None
        self.used.add(name)
        return f"{self.base_url}img/{name}"

    def _convert(self, data: bytes, path: Path) -> bool:
        c = self.cfg
        img = Image.open(io.BytesIO(data))
        img.seek(0)  # first frame of GIFs
        if img.width < c["min_width"]:
            return False
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        img = img.convert("L" if c["grayscale"] else "RGB")
        if c["grayscale"] and c["gray_gamma"] != 1.0:
            img = img.point(lambda v: round(255 * (v / 255) ** c["gray_gamma"]))
        if img.width > c["max_width"]:
            img = img.resize((c["max_width"], round(img.height * c["max_width"] / img.width)), Image.LANCZOS)
            if c["sharpen"]:
                img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=70, threshold=3))
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "JPEG", quality=c["quality"], optimize=True)
        return True

    def prune(self) -> None:
        if self.out_dir.exists():
            for f in self.out_dir.glob("*.jpg"):
                if f.name not in self.used:
                    f.unlink()


# ---------------------------------------------------------------- We-MP-RSS

def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def fetch_feeds(http: httpx.Client, base: str) -> list[dict]:
    feeds, offset = [], 0
    while True:
        root = ET.fromstring(http.get(f"{base}/rss", params={"limit": 30, "offset": offset}).content)
        batch = [{"id": it.findtext("id"), "name": it.findtext("title") or "", "intro": it.findtext("description") or ""}
                 for it in root.iter("item")]
        feeds += batch
        if len(batch) < 30:
            return feeds
        offset += 30


def fetch_articles(http: httpx.Client, base: str, feed_id: str, limit: int) -> list[dict]:
    root = ET.fromstring(http.get(f"{base}/feed/{feed_id}.rss", params={"limit": limit}).content)
    articles = []
    for it in root.iter("item"):
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = datetime.now(timezone.utc)
        articles.append({
            "id": it.findtext("id") or "",
            "title": it.findtext("title") or "",
            "link": it.findtext("link") or "",
            "published": format_datetime(published),
            "content": it.findtext(f"{CONTENT_NS}encoded") or "",
        })
    return articles


def refresh_accounts(werss: str, feeds_meta: list[dict], cfg: dict) -> None:
    """Ask We-MP-RSS to pull new articles for the accounts that are due, gently enough to avoid WeChat's rate limit.

    Uses the per-account "update" endpoint (the same as the manual button in its UI), because the built-in scheduled
    task in current We-MP-RSS routes every MP_WXS_ feed to the WeRead collector and skips them without a WeRead cookie.
    """
    if not cfg["enabled"]:
        return
    if cfg.get("paused_until"):
        until = datetime.strptime(cfg["paused_until"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
        if datetime.now(timezone.utc) < until:
            print(f"（更新已暂停到 {cfg['paused_until']}，本次只同步已有文章）")
            return
    secrets_cfg = tomllib.loads(SECRETS_PATH.read_text(encoding="utf-8-sig")) if SECRETS_PATH.exists() else {}
    ak, sk = secrets_cfg.get("werss_ak", ""), secrets_cfg.get("werss_sk", "")
    if not (ak and sk):
        print("（secrets.toml 里没有 Access Key，跳过触发更新，只同步已有文章）")
        return

    now = time.time()
    state = json.loads(REFRESH_STATE_PATH.read_text(encoding="utf-8")) if REFRESH_STATE_PATH.exists() else {}
    last = state.setdefault("last_refresh", {})
    due = [f for f in feeds_meta
           if f["id"] != "MP_WXS_FEATURED_ARTICLES"  # hand-picked articles (add_article.py), nothing to crawl
           and now - last.get(f["id"], 0) >= cfg["min_hours"] * 3600]
    due.sort(key=lambda f: last.get(f["id"], 0))  # longest-waiting first
    due = due[: cfg["max_per_run"]]
    if not due:
        return

    http = httpx.Client(timeout=600, headers={"Authorization": f"AK-SK {ak}:{sk}"})
    for i, feed in enumerate(due):
        if i:
            time.sleep(cfg["gap_seconds"])
        print(f"  触发更新：{feed['name']} …", flush=True)
        try:
            resp = http.get(f"{werss}/api/v1/wx/mps/update/{feed['id']}", params={"start_page": 0, "end_page": 1})
            if resp.status_code in (401, 403):
                print("    ! Access Key 无效，请检查 secrets.toml")
                break
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"    ! 请求失败：{e}")
            break
        code, message = body.get("code"), body.get("message", "")
        if code == 0:
            # The endpoint only starts a background crawl and returns at once, so its result says nothing about
            # WeChat's rate limit; the pacing above (one account per run, each at most every min_hours) is the guard.
            last[feed["id"]] = time.time()
            print("    已提交，We-MP-RSS 在后台抓取；新文章会在之后的同步中出现")
        elif code == 40402:
            last[feed["id"]] = time.time()  # updated moments ago by someone else
        elif "Invalid Session" in message or "登录" in message:
            print(f"    ! 公众号后台授权已失效，请到 {werss} 重新扫码。本次停止触发更新。")
            break
        else:
            print(f"    ! 更新失败（{code}）：{message}")
    REFRESH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- output

def write_article(path: Path, feed_name: str, item: dict) -> None:
    date = parsedate_to_datetime(item["published"]).astimezone().strftime("%Y-%m-%d %H:%M")
    path.write_text(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{escape(item['title'])}</title><style>{PAGE_CSS}</style></head>
<body><main><h1>{escape(item['title'])}</h1>
<p class="meta">{escape(feed_name)} · {date}</p>
{item['html']}
<hr/><p class="meta">原文：<a href="{escape(item['link'])}">{escape(item['link'])}</a></p>
</main></body></html>
""", encoding="utf-8")


def write_feed(path: Path, feed: dict, feed_url: str, base: str) -> None:
    entries = []
    for it in feed["items"]:
        page = f"{base}{safe_id(it['id'])}.html"
        html = it["html"].replace("]]>", "]]]]><![CDATA[>")
        entries.append(f"""    <item>
      <title>{xml_escape(it['title'])}</title>
      <link>{xml_escape(page)}</link>
      <guid isPermaLink="false">wechat-{xml_escape(it['id'])}</guid>
      <pubDate>{it['published']}</pubDate>
      <description><![CDATA[{html}]]></description>
      <content:encoded><![CDATA[{html}]]></content:encoded>
    </item>""")
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{xml_escape(feed['name'])}</title>
    <link>{xml_escape(base)}</link>
    <atom:link href="{xml_escape(feed_url)}" rel="self" type="application/rss+xml"/>
    <description>{xml_escape(feed['intro'] or feed['name'])}</description>
    <language>zh-cn</language>
    <lastBuildDate>{feed['items'][0]['published'] if feed['items'] else 'Thu, 01 Jan 2026 00:00:00 +0000'}</lastBuildDate>
{chr(10).join(entries)}
  </channel>
</rss>
""", encoding="utf-8")


def write_index(out: Path, feeds: dict, base: str) -> None:
    rows = "\n".join(
        f'<li><b>{escape(f["name"])}</b>（{len(f["items"])} 篇）<br/><code>{escape(base + fid + "/feed.xml")}</code></li>'
        for fid, f in sorted(feeds.items(), key=lambda kv: kv[1]["name"]))
    (out / "index.html").write_text(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>公众号订阅</title><style>{PAGE_CSS} li{{margin:0 0 1.2em}} code{{word-break:break-all;font-size:.8em}}</style></head>
<body><main><h1>公众号订阅</h1><p class="meta">把下面的地址逐个添加到阅读器；也可以导入 <a href="feeds.opml">feeds.opml</a></p>
<ul>{rows}</ul></main></body></html>
""", encoding="utf-8")
    outlines = "\n".join(
        f'    <outline type="rss" text="{xml_escape(f["name"])}" title="{xml_escape(f["name"])}" xmlUrl="{xml_escape(base + fid + "/feed.xml")}"/>'
        for fid, f in feeds.items())
    (out / "feeds.opml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><head><title>公众号订阅</title></head><body>
{outlines}
</body></opml>
""", encoding="utf-8")


# ---------------------------------------------------------------- main

def ensure_secret_path(cfg: dict) -> str:
    if cfg["site"]["secret_path"]:
        return cfg["site"]["secret_path"]
    token = secrets.token_hex(12)
    text = CONFIG_PATH.read_text(encoding="utf-8-sig").replace('secret_path = ""', f'secret_path = "{token}"', 1)
    CONFIG_PATH.write_text(text, encoding="utf-8")
    print(f"已生成随机目录 {token}（写回 config.toml）")
    return token


def git_push() -> None:
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        print("（不是 git 仓库，跳过推送）")
        return
    git("add", "-A", "docs", "data")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("没有变化，不提交")
        return
    git("commit", "-m", f"sync: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if not git("remote").stdout.strip():
        print("已提交；还没配置远程仓库，跳过 push")
        return
    result = git("push")
    print("已推送" if result.returncode == 0 else f"! push 失败：{result.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    secret = ensure_secret_path(cfg)
    base = cfg["site"]["site_url"].rstrip("/") + f"/{secret}/"
    out = DOCS / secret
    src = cfg["source"]
    werss = src["werss_url"].rstrip("/")

    http = httpx.Client(timeout=120)
    try:
        feeds_meta = fetch_feeds(http, werss)
    except (httpx.HTTPError, ET.ParseError) as e:
        print(f"! 连不上 We-MP-RSS（{werss}）：{e}\n  确认 Docker Desktop 已启动、容器在运行。本次不做任何改动。")
        return 1
    if src["only"]:
        feeds_meta = [f for f in feeds_meta if f["name"] in src["only"]]
    print(f"We-MP-RSS 里有 {len(feeds_meta)} 个公众号")
    refresh_accounts(werss, feeds_meta, cfg["refresh"])

    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    images = ImageStore(out / "img", base, cfg["images"])
    new_state = {}
    for meta in feeds_meta:
        fid = safe_id(meta["id"])
        old = {it["id"]: it for it in state.get(fid, {}).get("items", [])}
        try:
            articles = fetch_articles(http, werss, meta["id"], src["items_per_feed"])
        except (httpx.HTTPError, ET.ParseError) as e:
            print(f"  ! {meta['name']} 获取失败：{e}（保留上次内容）")
            articles = []
        added = 0
        for art in articles:
            if art["id"] in old or not art["content"].strip():
                continue  # already synced, or We-MP-RSS hasn't fetched the body yet (picked up next run)
            cleaner = Cleaner(images.url_for)
            cleaner.feed(art["content"])
            old[art["id"]] = {k: art[k] for k in ("id", "title", "link", "published")} | {"html": cleaner.html()}
            added += 1
        items = sorted(old.values(), key=lambda it: parsedate_to_datetime(it["published"]), reverse=True)
        items = items[: src["items_per_feed"]]
        new_state[fid] = {"name": meta["name"], "intro": meta["intro"], "items": items}
        print(f"  {meta['name']}：新增 {added} 篇，共 {len(items)} 篇")

    # Images already referenced by kept articles must survive pruning even though we didn't re-download them.
    for feed in new_state.values():
        for it in feed["items"]:
            images.used.update(re.findall(r"/img/([0-9a-f]{20}\.jpg)", it["html"]))

    # rebuild output from scratch so removed accounts/articles disappear
    for fid_dir in out.iterdir() if out.exists() else []:
        if fid_dir.is_dir() and fid_dir.name != "img":
            for f in fid_dir.glob("*"):
                f.unlink()
            fid_dir.rmdir()
    for fid, feed in new_state.items():
        feed_dir = out / fid
        feed_dir.mkdir(parents=True, exist_ok=True)
        for it in feed["items"]:
            write_article(feed_dir / f"{safe_id(it['id'])}.html", feed["name"], it)
        write_feed(feed_dir / "feed.xml", feed, f"{base}{fid}/feed.xml", f"{base}{fid}/")
    out.mkdir(parents=True, exist_ok=True)
    write_index(out, new_state, base)
    images.prune()

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"订阅列表：{base}index.html")

    if cfg["git"]["push"] and not args.no_push:
        git_push()
    return 0


if __name__ == "__main__":
    sys.exit(main())
