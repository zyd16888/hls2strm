# jable-strm

抓取 Jable 的影片列表和详情，生成 Emby / Jellyfin 可以直接播放的 `.strm`（附带 nfo 和封面），带 Web 控制台。

- **不用浏览器**：curl_cffi 模拟浏览器指纹，多个域名自动轮换（默认先走镜像 `fs1.app`，被拦再换 `jable.tv`）；可选接入 Byparr / FlareSolverr 兜底
- **可观测**：Web 仪表盘显示各域名状态、队列、速率、失败原因和实时日志
- **可中断、可重试**：所有任务都存在 SQLite，进程被杀也能从断点续跑；失败按指数退避自动重试，也可以在页面上手动重试
- **可配置**：代理、限速、并发、重试策略、输出路径模板、播放模式等都在网页上改，改完即时生效
- **影片库**：每部影片可以查看并复制缓存的原站播放地址（CDN，显示剩余有效期）、原站页面链接、本服务地址和 strm 路径，也可以在网页里试播

## 工作原理

```
strm 内容：http://<服务地址>/play/IPZZ-983.m3u8
                 │
Emby/Jellyfin ──►│ /play  ──► 缓存的播放地址够用？ ──否──► 现抓详情页换新地址
                 │                     │
                 │   普通播放器：302 ──► mushroomtrack CDN
                 │   ffmpeg 类客户端：本服务中转 m3u8 和分片
```

几个实测得到的事实决定了这个设计：

1. 详情页里的 `hlsUrl` 带签名和过期时间戳，**约 3 小时失效**，所以 strm 不能直接写 CDN 地址。
2. 播放地址**不绑 IP**，可以 302 让播放器直连 CDN。
3. CDN **拒绝 User-Agent 含 `Lavf`（ffmpeg 默认 UA）或 `python-requests` 的请求**。Emby/Jellyfin 服务端用 ffmpeg 探测和转封装，所以这类请求会自动改由本服务中转；其余客户端照常 302。中转时分片遇到 403 会自动换新地址重试，看到一半地址过期也不会断。
4. 列表页使用 KVS 的异步块接口（`?mode=async&function=get_block…`），每页 24 部。最新更新约 1641 页，约 3.9 万部。

## 快速开始

### Docker（推荐）

```bash
# 先改 docker-compose.yml 里的 JABLE_UI_PASSWORD、JABLE_PUBLIC_BASE_URL 和 strm 输出目录
docker compose up -d --build
# 需要兜底解题服务时：docker compose --profile solver up -d
```

打开 `http://服务器:8080`，用 `admin` 和你设置的密码登录。

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate      # Windows：.venv\Scripts\activate
pip install -e ".[dev]"
JABLE_DATA_DIR=./data JABLE_UI_PASSWORD=xxx python -m jable_strm
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JABLE_DATA_DIR` | `data` | 数据库、日志、快照目录 |
| `JABLE_HOST` / `JABLE_PORT` | `0.0.0.0` / `8080` | 监听地址 |
| `JABLE_UI_USER` / `JABLE_UI_PASSWORD` | `admin` / 空 | Web 控制台的 Basic 认证；密码为空则不认证（启动日志会提示） |
| `JABLE_OUTPUT_DIR` | `数据目录/strm` | strm 输出目录（设置页可覆盖） |
| `JABLE_PUBLIC_BASE_URL` | `http://127.0.0.1:端口` | 写进 strm 的服务地址（设置页可覆盖） |
| `JABLE_LOG_LEVEL` | `INFO` | 日志级别 |

其余设置都在 Web「设置」页修改，保存在数据库里。

## 使用流程

1. 在「设置」里确认**对外地址**（Emby/Jellyfin 能访问到的本服务地址）、**输出根目录**和**代理**。
2. 在「输出库与订阅」里，对默认订阅「全站：最新更新」点「首轮全量」。流程是先翻完全部列表页、马上写 strm（每秒 1 次请求约 30 分钟），再逐部补详情、写 nfo、下载封面（约 11 小时）。中途可以暂停、取消或重启，都能续跑。
3. 在 Emby/Jellyfin 里给每个输出库各建一个「电影」媒体库，比如 `{输出根目录}/全部`、`{输出根目录}/中文字幕`。
4. 首轮全量完成后，订阅会按周期（默认 60 分钟）自动增量：从第 1 页往后翻，连续遇到 48 部已在该库里的影片就停。

### 输出库与订阅

- **输出库**：一个名称加一个目录。目录可以写相对输出根目录的路径，也可以写绝对路径；还可以单独设置路径模板。库目录之间不能互相嵌套。默认库叫「全部」，目录是 `全部`。
- 同一部影片可以同时出现在多个库里。nfo 每个库各写一份；封面在同一文件系统下用硬链接，不额外占空间。
- **订阅**：列表来源 + 输出库 + 周期。第一次跑「首轮全量」，翻完全部页；之后按周期增量。新建时不勾「首轮全量」，就只跟进以后的更新。
- 例子：新建库「中文字幕」（目录 `中文字幕`），再新建订阅，来源填 `/categories/chinese-subtitle/`，输出库选「中文字幕」。
- 修改库的目录或模板时，会自动排一个重写任务，把已有文件搬到新位置。删除库时可以选择是否连同文件一起删除。

### 其他任务类型（「任务」页）

- **列表地址**：一次性抓取分类 `/categories/x/`、标签 `/tags/x/`、女优 `/models/x/`、搜索 `/search/关键词/`、热门 `/hot/`，可以指定排序、页码范围和输出库
- **指定影片**：粘贴影片网址或 slug（每行一个），加入所选的输出库
- **补全缺失详情**：给所有缺详情的影片排队
- **重写输出**：改了对外地址、播放模式、令牌或路径模板后执行（不联网），可以只重写某个库

## 输出

默认路径模板是 `{slug}/{slug}`，生成的文件如下：

```
<输出根目录>/全部/IPZZ-983/IPZZ-983.strm         http://<服务地址>/play/ipzz-983.m3u8
                         IPZZ-983.nfo          标题、番号、女优、类型、标签、上市日期、时长
                         IPZZ-983-fanart.jpg   原始封面（DVD 封套）
                         IPZZ-983-poster.jpg   从封面右侧裁出的竖版海报
```

模板可用的变量有 `{slug}`、`{code}`、`{actor}`、`{year}`，必须包含 `{slug}`，例如 `{actor}/{slug}`。模板改了以后执行「重写输出」，nfo 和封面会跟着搬到新位置，旧目录会被清理。

## 播放模式

| 模式 | 行为 |
|---|---|
| `redirect`（默认） | 302 到 CDN；UA 命中「中转 UA 片段」（默认 `Lavf`、`python-requests`）的客户端改走中转 |
| `proxy` | 全部经本服务中转，视频流量都经过本机 |
| `direct` | strm 直接写 CDN 地址，约 3 小时后失效，只用于调试 |

设置了「播放令牌」后，strm 地址会带上 `?t=令牌`，不带令牌的 `/play` 请求返回 403。

## 被拦截了怎么办

- 某个域名被拦截时，它会进入冷却（默认 5 分钟，连续被拦就翻倍，最长 1 小时），限速减半，并切换到下一个域名。
- 所有域名都被拦截时，引擎暂停，到冷却结束再自动重试；概览页会显示倒计时。也可以点「测试连通性」或「重置冷却」立即重试。
- 如果一直被拦：
  - 在「设置 → 站点域名」里加入新的镜像域名，或者换一个代理。
  - 也可以启用 Byparr，并在「设置 → 解题服务地址」填 `http://byparr:8191`。

## API

所有 `/api/*` 接口都和 Web 控制台使用同一套认证；`/play` 和 `/hls` 不需要认证，可用播放令牌保护。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 引擎、域名、队列、计数、最近失败 |
| POST | `/api/jobs` | 新建任务：`{"kind": "list\|videos\|backfill\|rewrite", "library_id": 1, ...}` |
| GET | `/api/jobs`、`/api/jobs/{id}/tasks?status=failed` | 任务列表、子任务列表 |
| POST | `/api/jobs/{id}/pause\|resume\|cancel\|retry` | 控制任务 |
| GET | `/api/videos?q=&filter=no_detail&library_id=&page=` | 影片库（每部影片带所在的库和 strm 路径） |
| GET / POST / PUT / DELETE | `/api/libraries`、`/api/libraries/{id}` | 输出库；DELETE 可带 `?delete_files=true` |
| GET / POST / PUT / DELETE | `/api/subscriptions`、`/api/subscriptions/{id}` | 订阅 |
| POST | `/api/subscriptions/{id}/run?mode=auto\|full\|incremental` | 立即运行订阅 |
| POST | `/api/videos/{slug}/refresh` | 立即重抓详情 |
| GET / PUT | `/api/settings` | 读取或修改设置（PUT 只需要传改动的字段） |
| POST | `/api/engine/pause\|resume`、`/api/fetcher/test\|reset` | 引擎和抓取通道控制 |
| GET | `/api/logs/stream` | 实时日志（SSE） |
| GET/HEAD | `/play/{slug}.m3u8` | strm 指向的播放入口（`?proxy=1` 强制中转） |

## 开发

```bash
pip install -e ".[dev]"
pytest
```

代码结构（`jable_strm/`）：

| 模块 | 职责 |
|---|---|
| `fetcher.py` | 分层抓取：多域名轮换、冷却、自适应限速、拦截判定、Byparr/FlareSolverr |
| `parser.py`、`sources.py` | 列表页和详情页解析、列表地址规范化、分页 URL 构造 |
| `engine.py` | 持久化队列、worker、重试策略、暂停/恢复、定时增量 |
| `writer.py` | strm、nfo、封面输出 |
| `play.py` | 播放地址缓存与换新、302 和中转 |
| `db.py`、`config.py`、`observability.py` | 存储、设置、日志与指标 |
| `api.py`、`static/` | Web 控制台 |

测试用的页面样本在 `tests/fixtures/`。站点改版导致解析失败时，失败页面会被存到 `数据目录/snapshots/`，任务页的错误信息里有对应链接。
