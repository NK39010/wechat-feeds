# wechat-feeds

把本机 We-MP-RSS 抓到的公众号文章，整理成每个公众号一个 RSS，推送到 GitHub，经 GitHub Pages 发布给 reMarkable（镇纸）订阅。

```
We-MP-RSS（本机 Docker, :8001）
  → sync.py：清掉微信排版、图片下载并按墨水屏处理（1404px 灰度）
  → docs/feeds/<公众号>/feed.xml
  → git push → https://nk39010.github.io/wechat-feeds/
```

reMarkable 读的是公网地址，不需要和电脑在同一个 WiFi；电脑关机期间不更新，开机后自动补上。

## 一次性设置

1. **We-MP-RSS 跑起来**：见 `../we-mp-rss/README.md`，扫码并添加好公众号。
2. **建仓库**：GitHub 上新建公开仓库 `wechat-feeds`，不要勾选 README。然后在本目录：
   ```bash
   git remote add origin https://github.com/NK39010/wechat-feeds.git
   git push -u origin main
   ```
3. **开启 Pages**：仓库 Settings → Pages → Deploy from a branch，分支 `main`，目录 `/docs`。
4. **跑一次**：`.venv\Scripts\python.exe sync.py`，输出里会打印订阅列表地址。
5. **定时同步**：`powershell -ExecutionPolicy Bypass -File .\register_task.ps1`（登录后每小时一次）。

## 订阅

打开 https://nk39010.github.io/wechat-feeds/feeds/index.html ，把每个公众号的地址加到镇纸（或导入 `feeds.opml`）。

## 注意

- **授权过期**：We-MP-RSS 扫码授权几天会过期，过期后没有新文章，回管理页重扫。
- **正文还没抓到的文章会先跳过**，下一轮同步时补上。
- 已同步的文章不会因为公众号删文而消失（最多保留 `items_per_feed` 篇）。
