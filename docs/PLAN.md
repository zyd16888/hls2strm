# Jable → STRM 方案（v2，已实现）

> 2026-10-05。v2 按反馈调整：浏览器只作最后兜底，部署在 Linux 服务器上。已确认：Emby + Jellyfin、抓全站、默认 302 跳转。
> M1–M4 已全部实现，使用说明见 README。实现中的新发现和与原方案的差异见文末「实现记录」。

## 1. 实测结论

| 项目 | 结论 | 对设计的影响 |
|---|---|---|
| jable.tv 主站 | Cloudflare 防护。curl、curl_cffi（各种浏览器指纹）都返回 `403 cf-mitigated: challenge`；请求一多，同一 IP 会被升级成 Turnstile 人工勾选 | 主站只作备用 |
| **fs1.app 镜像** | **同一套 KVS 站点，curl_cffi 和普通 httpx 都能直接拿到 200**。主站已经把这个 IP 拉进人工验证时，镜像仍然正常。按每秒 1 次连续请求 12 页，全部成功，单页约 0.3 秒 | **主力抓取渠道**，不需要浏览器 |
| 播放地址 | 详情页内联 `var hlsUrl = 'https://<子域>.mushroomtrack.com/hls/{token}/{expires}/62000/62398/62398.m3u8'`。`expires` 约 3 小时后到期，改动会返回 403（有签名） | strm 写本服务的地址，播放时现取新地址 |
| IP 绑定 | **不绑 IP**：本机拿到的 m3u8 地址，换一个出口 IP（外部服务器）也能直接拉取 | **302 跳转模式可行** |
| CDN | m3u8、key、ts 不校验 Referer，也不校验 TLS 指纹；AES-128 加密，key 是相对路径 | 播放器（ffmpeg）能直接处理 |
| **CDN 的 UA 黑名单** | User-Agent 含 `Lavf`（ffmpeg 默认 UA）或 `python-requests` 时，m3u8、分片和 key 都返回 403；`FFmpeg/`、`Emby/`、`Kodi/`、`VLC/`、浏览器等都能通过 | Emby/Jellyfin 服务端用 ffmpeg 探测和转封装，纯 302 会失败，**这类 UA 自动改走中转** |
| 跨域 | CDN 不返回 CORS 头 | 网页试播必须走中转（同源） |
| 下架影片 | 详情页返回 200，但只是兜底页（标题是 `%title%`，没有播放器） | 识别为下架，不当成解析失败 |
| 列表接口 | KVS 的异步块接口 `?mode=async&function=get_block&block_id=list_videos_latest_videos_list&sort_by=post_date&from=N` 可用，只返回列表片段（约 36 KB，整页约 80 KB）；最新更新共 1641 页 × 24 条 ≈ 3.9 万部 | 全站列表用这个接口跑 |
| 封面 | 主站是 `assets-cdn.jable.tv`，镜像是 `assets.fs1.app`，普通 HTTP 都能拿 | 封面下载不受 CF 影响 |

能顺手拿到的数据：

- **列表页**：videoId、slug、番号+标题、时长、缩略图、预览 mp4、观看数、点赞数
- **详情页**：标题、女优（ID、名字）、分类、标签、上市日期、观看数、收藏数、画质标签（高清原片、中文字幕等）、封面、hlsUrl
- **m3u8**：精确时长、分片数

## 2. 参考项目

| 项目 | 过 CF 的方式 | 可借鉴点 |
|---|---|---|
| [xin0907/vget-cli](https://github.com/xin0907/vget-cli) | curl_cffi `impersonate="chrome"`，被拦就换镜像 `fs1.app` | 镜像故障转移；用 `cf-mitigated` 头和页面特征判断是否被拦 |
| [shurgogo/Jable-Missav-Supjav-Downloader](https://github.com/shurgogo/Jable-Missav-Supjav-Downloader) | Tauri 内嵌 webview；默认域名列表就有 `fs1.app` | 可配置的域名列表；排序参数（post_date、video_viewed 等）；分类、标签表 |
| [jooservices/go-jabledownloader](https://github.com/jooservices/go-jabledownloader) | chromedp 无头 Chrome + 持久 profile | 站点接口统一抽象成 List / Search / Detail；用 HTML 样本做测试 |
| [ThePhaseless/Byparr](https://github.com/ThePhaseless/Byparr) | 独立服务（Playwright + invisible-playwright，会自动点 Turnstile），接口兼容 FlareSolverr `POST /v1` | 兜底解题器，返回 cookies、userAgent 和 HTML；支持按请求指定代理 |
| [fe80Grau/ytdlp2STRM](https://github.com/fe80Grau/ytdlp2STRM) | — | strm 写本服务的动态地址，播放时再解析 |

## 3. 总体架构

```
          Web UI（仪表盘 / 任务 / 影片库 / 设置）   REST + SSE
                          │
┌─────────────────────────▼─────────────────────────────────────────┐
│ jable-strm（FastAPI 单进程，Docker）                                 │
│  任务引擎：SQLite 持久化队列 · 自适应限速 · 重试 · 暂停/恢复 · 断点续跑  │
│     列表任务 ─► 详情任务 ─► 写 strm / nfo / 封面                        │
│  分层抓取器 Fetcher ─────────────────────────────── 代理 ─► fs1.app / jable.tv
│     L1 curl_cffi 多域名轮换  →  L2 Byparr 拿 cookie / HTML（可选）      │
│  播放解析器 /play/{slug}.m3u8 → 302 到新鲜 CDN 地址                    │
└────────────────────────────────────────────────────────────────────┘
       ▲ strm：http://<服务地址>/play/IPZZ-983.m3u8         │ 302
  Emby / Jellyfin ──────────────────────────────────────────▼──► mushroomtrack CDN
```

不内置浏览器。Byparr 作为可选的独立容器，只在 L1 全部失败时才用。

## 4. 关键设计

### 4.1 分层抓取器

- **L1：curl_cffi**（`AsyncSession`，`impersonate="chrome"`，走配置的代理）
  - 域名列表可配置，默认 `fs1.app`、`jable.tv`，按健康度排序。
  - 某个域名被拦（`cf-mitigated`、403/429/503、页面是 "Just a moment"）时，该域名进入冷却（指数退避），自动切到下一个。
- **L2：Byparr / FlareSolverr**（可选，配置了地址才启用）
  - L1 全部被拦时，请 Byparr 打开目标页，拿回 `cf_clearance` 和 userAgent，注入 L1 的会话后重试。
  - 注入后还是被拦，就直接用 Byparr 返回的 HTML（慢，只作兜底）。
  - 代理通过 `X-Proxy-Server` 请求头逐次传给 Byparr，和本服务保持同一个出口。
- **全部失败**：引擎自动暂停，UI 告警；每隔 N 分钟探测一次，恢复后自动继续。
- **限速**
  - 全局令牌桶，默认每秒 1 次，并发 2（可调）。
  - 自适应：被拦时速率减半并冷却；连续成功后缓慢回升，最高不超过配置值。

### 4.2 全站抓取

1. **列表阶段**：用异步块接口按 `post_date` 翻完 1641 页。每拿到一条就入库并写 strm（只需要 slug）。每秒 1 次约 30 分钟跑完。
2. **详情阶段**：逐个抓详情页，补齐元数据并写 nfo、下载封面。3.9 万页按每秒 1 次约 11 小时，可以断点续跑。
3. **增量**：定时（默认每小时）从第 1 页往后翻，连续遇到 N 条已入库的就停。新影片走同样的列表 → 详情 → 输出流程。
4. **翻页漂移**：全站扫描期间有新片插入，会让翻页错位。处理方式：按 videoId 去重，扫完后补跑一轮增量。

其他任务来源：热门、分类、标签、女优、搜索词、单个 URL（共用同一套任务模型）。

### 4.3 播放解析（默认 302）

- strm 内容：`{public_base_url}/play/{slug}.m3u8`，可选加访问 token。
- 文件名用 slug（例如 `IPZZ-983`、`SONE-001-C`），这样中文字幕版这类变体不会互相覆盖。
- 收到 `/play` 请求时：
  - 缓存的 hlsUrl 剩余有效期 ≥ 影片时长 + 15 分钟，直接 302。
  - 否则现抓一次详情页（插到队列最前，耗时不到 1 秒），更新缓存后再 302。
  - 同一部影片同时来的多个请求合并成一次抓取。
- 备选模式（设置里切换）：
  - **代理中转**：播放端连不上 CDN 时使用，流量走本服务。
  - **直写 CDN 地址**：仅调试用。

### 4.4 输出（Emby / Jellyfin）

- 目录模板可配置，默认 `{output}/{slug}/{slug}.strm`。
- `{slug}.nfo` 用 Kodi movie 格式，Emby 和 Jellyfin 都认。内容包括标题、番号、女优、类型、标签、上市日期、时长；时长写进去，播放器就不用再探测。
- 封面：`fanart.jpg` 用原图；`poster.jpg` 裁右半边，可关闭。全站约 3.9 万张，每张约 140 KB，合计约 5.5 GB，所以封面下载做成开关。
- 原子写入（先写临时文件再重命名）；内容没变就不重写。

### 4.5 可靠性：失败、中断、重试

- 所有任务都落在 SQLite：`pending → running → done / failed / gone`。
- 不同错误的处理方式：

  | 错误 | 处理 |
  |---|---|
  | 网络错误、超时、5xx | 指数退避重试，默认 5 次（30 秒、2 分、10 分、30 分、2 小时） |
  | 404 | 标记为 `gone`，不再重试 |
  | 被拦截 | 不计入失败次数，交给抓取器换域名或冷却 |
  | 解析失败 | 标记为 `failed`，保存 HTML 快照方便排查 |

- 中断恢复：重启时把 `running` 改回 `pending`，从断点继续。收到 SIGTERM 会优雅退出。
- 幂等：影片按 videoId upsert。

### 4.6 代理

- 抓取代理支持 `http://`、`socks5://`、`socks5h://`，可以带账号密码（curl_cffi 原生支持）。这个代理会同时传给 Byparr。
- 封面下载可以单独设置代理，默认跟抓取代理一致。
- 302 模式下，CDN 流量不经过本服务。

### 4.7 可观测与 Web 控制

- **仪表盘**
  - 各域名的健康状态（正常 / 冷却中 / 被拦）、Byparr 状态、引擎状态
  - 计数：影片、strm、待处理、失败、被拦次数
  - 当前速率、最近错误、实时日志（SSE）
- **任务页**：新建任务（全站、增量、分类、标签、女优、搜索、单个 URL）、进度条、暂停/恢复/取消、重试失败项；每个子任务的错误和耗时都能看到
- **影片库**：搜索和筛选、hlsUrl 剩余有效期；单条可刷新、重写 strm、在网页里试播（hls.js）
- **设置**：域名列表、代理、限速、并发、重试策略、Byparr 地址、输出目录和模板、播放模式、对外地址、定时增量周期、封面开关。大部分改完即时生效。
- **日志**：输出到 stdout（方便 docker logs）、滚动文件，以及内存环形缓冲（推送给 UI）。

## 5. 技术栈

| 层 | 选型 |
|---|---|
| 运行时 | Python 3.13，Docker 镜像 `python:3.13-slim` |
| Web | FastAPI + Uvicorn |
| 存储 | SQLite（SQLAlchemy 2 async + aiosqlite，WAL 模式） |
| HTTP | curl_cffi（站点、CDN、封面统一用它） |
| 解析 | selectolax（lexbor 后端） |
| 前端 | Jinja2 + HTMX + Alpine.js，不需要构建 |
| 测试 | pytest，用保存的 HTML 样本做解析器单测 |
| 兜底解题 | Byparr 容器（可选） |

## 6. 目录结构（实际）

```
jable/
  pyproject.toml  Dockerfile  docker-compose.yml  README.md
  jable_strm/
    __main__.py      # python -m jable_strm
    app.py           # 组件装配 + lifespan
    config.py        # 启动配置（环境变量）+ 运行时设置（数据库，可在网页上改）
    db.py            # SQLite：settings / videos / jobs / tasks
    fetcher.py       # 分层抓取（含 Byparr/FlareSolverr 客户端）
    parser.py        # 列表页 / 详情页解析（纯函数）
    sources.py       # 列表地址规范化、异步块分页 URL
    engine.py        # 队列、worker、重试、暂停/恢复、定时增量
    writer.py        # strm、nfo、封面
    play.py          # 播放解析器 + /play、/hls 路由
    api.py           # Web 控制台 JSON API + SSE 日志
    observability.py # 日志（stdout、滚动文件、环形缓冲）+ 指标
    static/          # index.html、app.js、app.css、vendor/alpine.min.js
  tests/             # pytest + fixtures/*.html
```

## 7. 分期

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 打通链路 | 抓取器 L1 + 解析器 + 写 strm + `/play` 302 | ✅ 本地实测：ffprobe / ffmpeg 通过 strm 地址拉流成功；Emby/Jellyfin 还需在服务器上实测 |
| M2 引擎和全站 | 持久化队列、自适应限速、重试、断点续跑、全站列表 + 详情、定时增量、nfo、封面 | ✅ 实测强杀进程后重启能续跑；约 500 部按每秒 1 次连续抓取未被拦截 |
| M3 Web 和部署 | 仪表盘、任务、影片库、设置、实时日志；Dockerfile + compose | ✅ 页面已在浏览器里验证；Docker 镜像未在本机构建（本机没有 Docker） |
| M4 兜底 | Byparr 接入、代理中转播放模式 | ✅ 已实现；Byparr 未实测（镜像站没被拦，暂时用不到） |

## 8. 风险与待验证

- **镜像可能失效或加防护**：域名列表可配置，自动切换；最坏情况由 Byparr 兜底。
- **长时间持续抓取的限速阈值未知**：自适应限速，被拦就降速，在 M2 实际跑全站时校准。
- **播放端能否直连 mushroomtrack CDN**：302 模式要求 Emby/Jellyfin（或客户端）能访问 CDN。服务器在国内时可能要给 Emby 配代理，或改用代理中转模式。
- **Emby/Jellyfin 扫库时会不会探测 strm**：如果会，首次扫描约 3.9 万次请求都会打到 `/play`。M1 验证；必要时对探测请求直接返回缓存，或者限流。
- **token 约 3 小时过期**：302 前保证剩余有效期 ≥ 时长 + 15 分钟。暂停好几个小时后再继续播放会失败，需要重新点播放。

## 9. 实现记录

- **按 UA 分流**：见上方「CDN 的 UA 黑名单」。这是 302 模式下 Emby/Jellyfin 能播放的关键，UA 片段在设置里可以改。
- **中转播放列表用相对地址**（`../hls/{slug}/{file}`）：不依赖「对外地址」的写法，经反向代理加路径前缀也能用。
- **自动定时增量的触发条件**：全站任务完成过，或者手动跑过增量。避免在库还不完整时，增量把前 20 页当新片全部抓一遍。
- **超时视为软限流**：镜像偶尔出现 30 秒无响应，此时限速降到 3/4，连续成功后再慢慢回升。
- **代码结构**：原计划分子包，实际采用单层模块（规模不大，单层更直观）。
