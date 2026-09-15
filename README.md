# wechat-feeds

把本机 We-MP-RSS 抓到的公众号文章，整理成每个公众号一个 RSS，推送到**私有仓库**，经 Cloudflare Pages 发布给 reMarkable（镇纸）订阅。

```
We-MP-RSS（本机 Docker, :8001）
  → sync.py：清掉微信排版、图片下载并按墨水屏处理（1404px 灰度）
  → docs/<随机目录>/<公众号>/feed.xml
  → git push 到私有仓库 → Cloudflare Pages
```

reMarkable 读的是公网地址，不需要和电脑在同一个 WiFi；电脑关机期间不更新，开机后自动补上。

## 一次性设置

1. **We-MP-RSS 跑起来**：见 `../we-mp-rss/README.md`，扫码并添加好公众号。
2. **建私有仓库**：GitHub 上新建仓库（例如 `wechat-feeds`），**选 Private**，不要勾选 README。然后在本目录：
   ```bash
   git remote add origin https://github.com/<用户名>/wechat-feeds.git
   git push -u origin main
   ```
3. **Cloudflare Pages**：注册/登录 Cloudflare → Workers & Pages → Create → Pages → Connect to Git，选这个仓库。
   - Framework preset: `None`，Build command 留空，Build output directory: `docs`
   - 部署完成后得到 `https://<项目名>.pages.dev`，填到 `config.toml` 的 `site_url`
4. **跑一次**：`.venv\Scripts\python.exe sync.py`，输出里会打印订阅列表地址。
5. **定时同步**：`powershell -ExecutionPolicy Bypass -File .\register_task.ps1`（登录后每小时一次）。

## 订阅

打开 `https://<项目名>.pages.dev/<secret_path>/index.html`，把每个公众号的地址加到镇纸（或导入 `feeds.opml`）。

`secret_path` 是首次运行时生成的随机字符串（在 `config.toml` 里）。仓库私有 + 地址不可猜，别人既看不到代码也找不到内容；但**知道地址的人就能访问**，别把订阅地址分享出去。

## 注意

- **pages.dev 在国内可能访问不稳**：先确认 reMarkable 能打开订阅地址；不行的话给 Cloudflare Pages 绑定自己的域名。
- **授权过期**：We-MP-RSS 扫码授权几天会过期，过期后没有新文章，回管理页重扫。
- **正文还没抓到的文章会先跳过**，下一轮同步时补上。
- 已同步的文章不会因为公众号删文而消失（最多保留 `items_per_feed` 篇）。
