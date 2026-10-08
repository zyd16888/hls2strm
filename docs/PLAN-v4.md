# v4 方案：多站点（同番号多源择优 + 扩充资源）

> **实施状态（2026-10-08）**：M1–M3 已完成，SupJav 提前到这一期一起接入（线路 EVS、FST、VOE、ST）。和方案的差异见文末「实施记录」。
> 待确认的几项按推荐做：变体按「番号 + 是否无码流出」；字幕默认允许回退；网关场景两种都支持（填了公网中转地址就返回中转链接，没填返回 409）；站点地图导入没做。
> 实测脚本和输出在 `.tmp/probes/ms2/`（补充实测）和 `.tmp/probes/multisite/`（子代理的第一轮探测），参考项目源码在 `.tmp/refs/`。

## 1. 调研结论

### 1.1 三个参考项目

| 项目 | 实际内容 | 能借鉴的 |
|---|---|---|
| aizhimou/jav-play-go | **没有 Go 代码**，只有两个油猴脚本（在 JavDB 页面加跳转 MissAV 的链接；去掉 MissAV 的弹窗广告）和一条去广告规则 | 只说明 MissAV 能用 `/{番号}` 直接打开 |
| shurgogo/Jable-Missav-Supjav-Downloader | Jable、MissAV、SupJav；页面全部走隐藏 WebView，碰到 CF 弹窗让人手动验证 | MissAV、SupJav 的选择器、列表路径、排序参数；SupJav 线路解析规则 |
| xin0907/vget-cli | Jable、MissAV、SupJav、Hanime1；curl_cffi + 镜像故障转移 | Extractor 抽象（结果带请求头、分片变换、备用源）；packer 解包；按分辨率选码率 |

### 1.2 候选站点实测（2026-10-08，本机直连，curl_cffi `impersonate="chrome"`）

| 站点 | 页面能否直接抓 | 播放地址 | 播放器能否直连 | 结论 |
|---|---|---|---|---|
| **MissAV** | 主域 missav.ws/.ai 的首页和列表页是 CF 挑战；详情页一开始能拿，同一 IP 请求十几次后也被挑战。**镜像 missav123.com、missav.live 的列表、搜索、详情都直接 200**，missav123.com 连续近 30 次没被拦 | `surrit.com/{uuid}/playlist.m3u8`，没有签名和过期时间（缓存头 1 年，相隔 40 分钟取到同一个 uuid）；多码率 360p–720p | **不能**：要 Referer 是 missav 的域名，还要浏览器 TLS 指纹（不是看 UA） | **接入**：做备源和资源扩充 |
| SupJav | 整站 CF 挑战（首页、搜索、分类、feed 都是 403） | 详情页 id 是数字，番号只能靠搜索；播放要先过 `lk1.supremejav.com` 网关，再进 TV/FST/ST/VOE 等第三方播放器，FST 分片还带假 PNG 头 | 看线路 | 暂不接：要 Byparr，链路长，容易坏 |
| Hanime1 | 直接 200 | mp4 直链，约 12 小时过期，不要 Referer | 能 | 动画站，没有番号，和"同番号择优"无关；要做动画库时再单独接 |
| JavDB | 直接 200 | 没有（只有元数据和磁力） | — | 不是播放源；以后可以当目录或元数据来源 |

### 1.3 MissAV 细节

- **详情页**：`{域名}/cn/{slug}`，`dmN/` 前缀可以省。
  - 不存在的番号返回 404。
  - 写错的 slug 会被纠正（Jable 的 `miaa-462bfwnhgfn` 会跳到 `miaa-462`），所以要以最终地址和页面里的番号为准。
- **播放地址**：页面里有两段 packer（`eval(function(p,a,c,k,e,d)`，进制最大 62），解包后是 `source='https://surrit.com/{uuid}/playlist.m3u8'`。
  - 列表卡片上也有一个 uuid，但那是预览用的，和播放 uuid 不同。所以每部片仍要抓一次详情，好在拿到后可以长期缓存。
- **surrit CDN**：

  | 请求 | 结果 |
  |---|---|
  | 浏览器指纹 + `Referer: https://missav.ws/`（或 missav.ai） | 200 |
  | 浏览器指纹 + 不带 Referer、Referer 是 example.com、或只带 Origin | 403 |
  | 浏览器指纹 + Referer + UA 改成 Lavf / Emby | 200 |
  | libcurl 默认指纹 + Referer + 任意 UA（包括 Chrome 的） | 403 |

  - 分片叫 `video0.jpeg`，Content-Type 也是 image/jpeg，实际是 TS（开头是 0x47 同步字节），没有加密。
  - 新版 ffmpeg 的 HLS 解复用会检查分片扩展名，`.jpeg` 可能被拒（Emby 自带的 ffmpeg 没测），所以中转时统一改名 `.ts`。
  - master 里的 `#EXT-X-TOKEN` 解出来是"出口 IP|数字|哈希"，可能绑定 IP。反正要中转，不影响。
  - 不返回 CORS 头。
- **变体**：每个变体是一个独立的 slug：`{番号}`、`{番号}-chinese-subtitle`、`{番号}-uncensored-leak`、`{番号}-english-subtitle`。还有 `kira-020-2` 这种分段。
- **列表**：
  - 入口：`/cn/new`（最近更新）、`/cn/release`（最新发行）、`/cn/chinese-subtitle`、`/cn/uncensored-leak`、`/cn/search/{关键词}`、`/cn/genres/…`、`/cn/actresses/…`、`/cn/makers/…`。
  - 参数：`?page=N&sort=released_at|published_at|saved|today_views|weekly_views|monthly_views|views`。
  - **每页 12 条，最多 2000 页**，所以单个列表最多 2.4 万部。
  - 卡片是 `div.thumbnail`，有 slug、标题、时长和封面 `fourhoi.com/{slug}/cover-t.jpg`（大图把 `-t` 换成 `-n`）。
- **站点地图**：`/sitemap.xml` 不被拦。
  - 共 517 个 `sitemap_items_N.xml`，每个 1000 部（每部 13 种语言各一条），合计约 51.6 万个 slug（含变体）。
  - 只有地址，没有元数据。每个文件解压后约 17 MB，用 br 压缩传输。
  - 最后几个文件里大多是新加的无码流出、英字变体，**不是按发行日期排的**，不能用来做新片增量。
- **元数据**（`/cn/` 页面）：中文标题、封面、时长（秒）、发行日期、女优、男优、类型、发行商、导演、标签、系列。

### 1.4 和 Jable 的重合

- 从 Jable 最近更新的第 1、300、1200 页抽了 16 部，**MissAV 上全都有**。
- MissAV 的中字版很少：抽查 4 个番号的 `-chinese-subtitle` 版，全是 404，其中 jul-624 在 Jable 上是中字片；站点地图最后一个文件的 204 部里，中字变体只有 1 部。也就是说，Jable 中字片在 MissAV 上找到的，通常是**无字幕原版**。
- Jable 的画质标签，目前只见到"中文字幕"和"高清原片"两种。

## 2. 目标

- **稳定性**：每部作品挂多个源，播放时按策略挑最好的，失败自动换下一个。库里已有的 Jable 作品，后台按番号去 MissAV 补源。
- **资源量**：MissAV 也能当列表来源（订阅最新、中字、无码流出、女优、类型、搜索）。新片如果 Jable 已经有了，就并成同一部作品。

## 3. 数据模型

核心改动是把**作品**和**源**分开：

- **作品**：一个番号，对应一个 strm。无码流出版单独算一部，见 3.2。
  - 沿用 `videos` 表和 id，`outputs` 等外键不用改。
  - strm 照样指向 `/play/{作品 slug}.m3u8`，老的 strm 一个都不用改。
- **源**：某个站点上的一个页面，存在新表 `sources` 里。Jable 的 `sone-001`、MissAV 的 `sone-001-chinese-subtitle` 都是源。

### 3.1 表结构

`videos` 的变化：

| 字段 | 说明 |
|---|---|
| `id` | 内部 id。老数据保留原值（就是 Jable 的 videoId），新作品自增 |
| `slug` | 作品 slug。老数据保持 Jable slug；新作品用规范番号的小写，无码流出版加 `-u` |
| `code`、`code_key` | 规范番号 `SSIS-001`；匹配键（大写、去掉分隔符、数字去掉前导零，FC2 / FC2PPV 统一） |
| `uncensored` | 是否为无码流出作品 |
| 元数据各列 | 合并后的元数据，见第 8 节 |
| `hls_url`、`hls_expires` | 搬到 `sources` |

`sources`（新表）：

| 字段 | 说明 |
|---|---|
| `id`、`video_id` | 源 id、所属作品 |
| `site`、`key` | 站点、站内 slug，`UNIQUE(site, key)` |
| `site_vid` | 站内数字 id（Jable 的 videoId；strm 扫描时用来识别 CDN 地址） |
| `subtitle` | `zh` / `en` / 空。Jable 看画质标签，MissAV 看 slug 后缀和类型 |
| `stream_url`、`stream_expires` | 缓存的播放地址；MissAV 的过期时间为空，表示长期有效 |
| `height` | 最高分辨率，解析 master 时顺便记下，可以为空 |
| `status` | `active` / `gone` / `disabled`（手动禁用） |
| `fail_streak`、`last_ok_at`、`last_fail_at`、`last_error` | 健康度 |
| `meta` | 这个站的原始元数据（JSON） |
| `detail_at`、`created_at`、`updated_at` | — |

另外：

- `source_checks(video_id, site, checked_at, found)` 表：记录"某站查过没有"，补源时不反复查。
- `tasks` 加 `site` 列。

迁移（v6）：

1. 建 `sources`，每部 Jable 影片生成一条 `site=jable` 的源：key 用 slug，site_vid 用 id，hls_url 搬过去。
2. 计算 `code_key`。
3. 设置里老的 `domains`、`rate_per_sec` 搬到 `sites.jable` 下。
4. Jable 自己有同番号的重复影片（比如 `miaa-462` 和 `miaa-462bfwnhgfn`）：迁移时不合并，各自保留原来的 strm。以后补源挂到 id 最小的那部上。

### 3.2 变体怎么归属（需要确认）

**推荐：作品由"番号 + 是否无码流出"决定，中字、英字只是源的属性。**

- 中字版和原版画面相同，只差字幕。在 Emby 里一部片一个条目更合理，而且这正是"同番号多源择优"要的效果：Jable 的中字源挂了，可以用 MissAV 的原版顶上。
- 无码流出版的画面不同。如果合并进同一部作品，"无码流出库"播出来可能是有码版，所以单独成一部，slug 加 `-u`（如 `fpre-176-u`），默认文件名里也带上。

另一种做法是每个变体各一个作品（中字、原版、无码流出各一个 strm）：Emby 里同一番号会出现好几条，mdcng 也会刮好几次。不推荐。

## 4. 站点适配器

每个站点一个模块，`sites/jable.py`、`sites/missav.py`，实现同一个接口：

```python
class Site:
    name = "missav"
    default_domains = ["https://missav123.com", "https://missav.live", "https://missav.ws"]
    stream = StreamTraits(headers={"Referer": "https://missav.ws/"}, impersonate=True, direct=False, expires=False)

    def detail_path(self, key: str) -> str
    def parse_detail(self, html: str, key: str) -> SourceDetail   # 番号、字幕和无码标志、元数据、播放地址
    def key_for(self, code: str, subtitle: str, uncensored: bool) -> str | None  # 按番号直接构造站内 key
    presets, sorts                                                 # 列表入口，给任务和订阅页面用
    def normalize_source(self, value: str) -> str
    def page_url(self, source: str, page: int, sort: str) -> str
    def parse_list(self, html: str) -> ListPage                    # [SourceItem(key, code, title, duration, cover…)]
```

- `codes.py`：番号规范化和匹配键。
  - 从标题或 slug 提取番号。
  - 处理 FC2 / FC2PPV / FC2-PPV、MissAV 的变体后缀、`-2` 这种分段。
  - 无码厂牌的日期型番号（如 `010120-001`）按原样比较，避免不同厂牌撞号。
- Jable 适配器：把现有的 `parser.py`、`sources.py` 搬进来，行为不变。
- MissAV 适配器：按番号直接拼 `/cn/{key}`，不需要搜索。

## 5. 抓取器按站点隔离

- 每个站点有自己的一套：域名列表和冷却、限速器、会话（可以用不同的模拟指纹）、是否启用解题服务。
- **一个站被拦，只停这个站的子任务**。现在是全局暂停，要改：
  - 引擎领任务时，跳过正被拦截的站，也跳过已经达到并发上限的站。每站并发默认 1–2。
  - worker 总数 = 各站并发之和。
- 设置结构改成 `sites: {jable: {enabled, domains, rate, concurrency, priority}, missav: {...}}`。

## 6. 播放：择优和故障切换

### 6.1 源的排序

先筛掉 `gone`、`disabled` 和所在站点未启用的源，剩下的依次比较：

1. **字幕偏好**：默认 中字 > 原版 > 英字，设置里可以调。也可以设成"只用首选字幕，不回退"。
2. **健康**：最近失败、还在冷却中的源排后面。冷却从 5 分钟起，连续失败翻倍，最长 6 小时。
3. **站点优先级**：默认 Jable > MissAV。原因是 Jable 能 302，不占本服务带宽，中字也多。
4. **现成地址**：缓存里有未过期地址的优先，省一次详情页请求。
5. **分辨率**：只在前面都相同时比较。Jable 的 m3u8 不写分辨率，不抓分片的话无法比较。

### 6.2 流程

1. 按顺序取源，拿播放地址：缓存够用就直接用，不够就抓这个站的详情页。同一个源的并发请求合并成一次。
2. 失败就换下一个：
   - 404 或下架：源标为 `gone`。
   - 被拦、超时、解析失败：`fail_streak` 加 1，进入冷却。
   - 一次播放请求的总时限默认 15 秒。
3. 已知的源都失败，而且开了"现场找源"：按番号去其他启用的站点直接拼地址试一次，找到了就加成新源再播。
4. 还是不行：所有源都下架时返回 404（作品标为 `gone`），其他情况返回 503。

另外两点：

- 刚抓到的新地址，可以先 GET 一次 m3u8 校验（默认开，超时 3 秒），挡掉 CDN 上已删的片。
- 指定源：`/play/{slug}.m3u8?src=missav` 强制用某个站，试播和排查时用。

### 6.3 302 还是中转

按选中的源所在站点决定：

| 源 | 普通播放器 | ffmpeg 类（UA 含 Lavf） | 浏览器跨域 |
|---|---|---|---|
| Jable | 302 | 中转 | 中转 |
| MissAV | **中转**（要 Referer 和浏览器指纹） | 中转 | 中转 |

中转要做成通用的。现在只支持单层 m3u8，而且假设分片和清单在同一目录，要改成：

- 支持 master → 子清单 → 分片的多层结构；
- 请求带上站点要求的请求头；
- `.jpeg` 改名 `.ts`，Content-Type 也改掉；
- 路径改为 `/hls/{slug}/{源 id}/{相对路径}`。

中转时分片返回 403：先换这个源的新地址重试一次（Jable 现在就是这样）。**播到一半不换站**：不同站点的切片不一样，换了也接不上。

**影响**：用 MissAV 源播放时，视频流量全部经过本服务。720p 每路约 2.8 Mbps（master 里的 BANDWIDTH），多人同时看要算服务器的带宽。

### 6.4 网关 resolve（需要确认）

- 网关只能 302，没法加 Referer，也没有浏览器指纹，所以 resolve 只挑能直连的源（目前只有 Jable）。
- 只有 MissAV 源的作品，可以二选一：
  - A：新增设置"公网中转地址"。填了之后，resolve 返回 `{公网中转地址}/play/{slug}.m3u8?t=…`，客户端经本服务中转播放。这要求本服务能从公网访问。
  - B：返回 409，让网关回退。按 README 里的说法，你的部署下回退后外部客户端会拿到内网地址，可能播不了。

## 7. 补源与扩充

### 7.1 补源（为了稳定性）

- 新任务类型"补源"：对全部作品或某个输出库里的作品，按番号到指定站点拼地址抓详情。
  - 404 表示没有，记进 `source_checks`，30 天后才重查。
  - 现有约 3.9 万部，按每秒 1 次大约 11 小时，可以只补某个库。
- 列表任务和订阅加一个选项"新片自动补源"：Jable 新片入库后，排一个低优先级的 MissAV 补源子任务。

### 7.2 MissAV 列表和订阅（为了资源量）

- 列表任务和订阅加"站点"选项。MissAV 的预设有：最近更新、最新发行、中文字幕、无码流出、搜索、女优、类型、发行商。
- 入库流程和 Jable 一样：按 `code_key` 加"是否无码流出"找作品，找到就挂源，找不到就新建作品。
- 写 strm 只需要 slug，所以翻列表时就能写。详情（播放 uuid 和完整元数据）有三种处理：
  - 翻完列表后补；
  - 播放时再抓（第一次播放多等约 1.5 秒，之后一直走缓存）；
  - 和现在的外部整理库一样，交给 mdcng 刮削。
- 量的估算：以中文字幕列表为例，2000 页 × 12 部 = 2.4 万部。只翻列表约 2000 次请求，按每秒 1 次约 35 分钟；如果还要抓详情，再加 2.4 万次请求，约 7 小时。

### 7.3 站点地图导入（可选）

- 用途：
  - 一次性建立"番号 → MissAV 有没有"的索引，补源时就不用逐部去试；
  - 或者按番号前缀、变体筛选后批量建作品，只写 strm，交给 mdcng 刮削。
- 量：517 个文件，解压后合计约 9 GB XML，要边下边解析。传输时有 br 压缩，实际下载量还没测。
- 不能做新片增量（原因见 1.3）。新片增量靠列表订阅。

## 8. 元数据与输出

- **作品的元数据**：取优先级最高、且已抓过详情的源（默认 Jable，其次 MissAV 的 `/cn/` 页面）。缺的字段用其他源补，比如 Jable 没有发行商、导演，MissAV 有。
- **nfo**：
  - `uniqueid` 改成 `type="num"`，值是番号；
  - 加 studio（发行商）、director（导演）、set（系列）；
  - 字幕和无码流出写成 tag。
- **封面**：优先用 Jable 的。MissAV 的是 `fourhoi.com/{slug}/cover-n.jpg`，也是 DVD 封套，同样能裁出 poster。
- **规则库**：
  - 女优、分类、标签改成按名字匹配，因为两个站的 slug 不一样；
  - 新增"站点""字幕""无码流出"三个条件。
- **strm 扫描**：surrit 地址也识别为 `cdn` 类，按 uuid 找到对应的源。

## 9. 界面

- **设置 → 站点**：每个站点可设置
  - 是否启用、域名、限速、并发；
  - 优先级（上下移动调整）；
  - 显示播放方式（302 还是必须中转），带"测试连通"按钮。
- **概览**：按站点显示状态、冷却时间和请求数。
- **影片库**：
  - 作品详情列出所有源：站点、字幕、状态、上次成功时间、地址剩余有效期、分辨率；
  - 每个源可以试播、禁用，也可以立即补源；
  - 新增筛选"只有一个源""某个站点"。
- **任务和订阅**：可以选站点；新增"补源""站点地图导入"两种任务。

## 10. 分期

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 重构，行为不变 | `codes.py`；`sources` 表和迁移；Site 接口 + Jable 适配器；抓取器、限速、拦截按站点拆开；播放改成按源取地址 | 现有测试全部通过；老数据库迁移后 strm 内容不变，能正常播放 |
| M2 MissAV 备源 | MissAV 适配器（镜像轮换、packer 解包、详情解析）；通用多层 HLS 中转；择优和故障切换；补源任务；现场找源；resolve 只给能直连的源；界面上的站点设置、源列表 | 禁用 Jable 站点后，库里的影片自动改用 MissAV 播放：ffprobe 通过 strm 地址能拉流，Emby 实测能播 |
| M3 MissAV 扩充 | MissAV 列表和订阅；新作品建档与合并；无码流出作品；规则和 nfo 扩展 | 订阅 MissAV 中文字幕列表，首轮全量和增量都能跑通 |
| M4 可选 | 站点地图导入；JavDB 作为目录；SupJav（需要 Byparr）；Hanime1 动画库 | 按需 |

M1 不加新站，只改结构，单独提交。这样即使后面的阶段有问题，现有功能也不受影响。

## 11. 风险与待验证

- **MissAV 的拦截**：主域上同一 IP 请求十几次就会被挑战；镜像 missav123.com 连续近 30 次没事，但长时间每秒 1 次能撑多久还不知道。应对方式和 Jable 一样：多域名轮换、冷却、自适应限速，必要时再上 Byparr。
- **镜像域名会变**：域名在设置里可以改。以后可以加"从页面里发现新镜像"。
- **surrit 的 uuid 能用多久**：只验证了 40 分钟内不变。所以不当成永久有效：中转失败时重新抓一次详情换地址。
- **Emby 自带 ffmpeg 能不能接受 `.jpeg` 分片**：没测。中转时统一改成 `.ts`，绕开这个问题。
- **中转带宽**：MissAV 源的视频都经过本服务，见 6.3。

## 待确认

1. **变体归属**（3.2）：作品由"番号 + 是否无码流出"决定，中字和英字作为源的属性。是否同意？
2. **字幕回退**（6.1）：Jable 中字源失效、只剩 MissAV 原版时，默认是否允许用原版顶上？
3. **网关场景**（6.4）：只有 MissAV 源的作品，选 A（配置公网中转地址）还是 B（返回 409）？
4. **扩充范围**（7.2、7.3）：先做 MissAV 列表订阅；站点地图导入（全站约 50 万条）要不要做？
5. **SupJav、Hanime1、JavDB** 都放到 M4 按需再做，可以吗？

## 实施记录

- **SupJav**：整站在 CF 后面，解题服务解一次后 cf_clearance + 解题服务的 UA 注入 curl_cffi 就能一直抓（UA 必须一致，TLS 指纹不影响）。
  番号靠站内搜索；标题前缀 [中文字幕] / [英文字幕] / [无码破解] 标出版本，[无码破解] 归到无码流出那部作品。
  播放：网关 lk1.supremejav.com（Referer supjav.com，302 到播放站）→ 播放站（Referer lk1）解出直链：
  EVS / FST 取 packer 里的 hls2，VOE 解混淆 JSON，ST 拼 robotlink 的 get_video（mp4）。直链都能 302，但带出口 IP / ASN，
  所以网关 resolve 不用它们。VAS（分片伪装成 .woff2）、LUC（只认浏览器）只能中转，没做。默认不启用。
- **站点接口多了 lookup**：能拼地址的站点（Jable、MissAV）拼 key 后抓详情核对番号；要搜索的站点（SupJav）返回核对过的搜索结果。
- **中转**：源目录以外或带查询串的地址签名后放进 `_x/`，并补 `.ts` / `.m3u8` 扩展名（实测 2025-08 版 ffmpeg：没有扩展名或者是 .jpeg 的分片，
  都报 not in allowed_segment_extensions；.ts 正常）；mp4 直链原样转发 Range。
- **curl_cffi 0.16 的流式响应**：`aclose()` 只等传输结束、不中止，客户端拖动或断开后会把整个文件下完堆在内存里（实测 100 MB 文件
  60 秒没关上）。关闭前先设 `quit_now` 才能立刻中止，中转的所有流都这样关。
- **MissAV 变体页的番号字段带后缀**（CJOD-538-CHINESE-SUBTITLE），去掉后再匹配。
- **实测**（本机）：Jable 老库从 v3 迁移到 v6 后 strm 不变、302 和中转都能播；MissAV 给 52 部 Jable 影片补源全部找到；
  禁用 Jable 后自动改用 MissAV 中转，ffprobe / ffmpeg（2025 版）能拉流；SupJav 用注入的 cookie 搜索、列表、详情都正常，
  FST / VOE / ST 的直链 302 和中转都能被 ffprobe 读出。没有 Byparr 环境，SupJav「解题 → 注入」这一步没有端到端实测。

### 第二轮（站点 → 线路，JavGuru、JAVMost、nJAV）

- **线路单独管理**：数据库 v7 的 source_lines 记每个源的线路（站点给的线路数据、认出的播放站、各自的直链缓存、
  中转要带的 Referer、失败冷却）；设置里每条线路可调顺序、启用、强制中转；网关只用能 302、不绑出口、没强制中转的线路。
- **嵌入播放站按页面内容识别**（sites/hosts.py），各站共用：VidHide / StreamHG、VOE、Streamtape、Vidara、LuluStream、
  MaxStream、TurboVip、Dood、DooPlayer。
- **SupJav**：ST 按用户实测改成不绑 IP（网关可用）；补上 VAS、LUC、TV；网关依次试 lk1 / lk2 / 根域名和 c= / l=。
- **JavGuru**：curl_cffi 直接能抓。线路数据是 base64 的 /searcho/?{L}d={HEX}，倒过来请求 ?{L}r= 拿到 302。
  用户说 JK 是直链：确实是 m3u8 直链（约 12 小时），但 MaxStream 的 CDN 要浏览器 TLS 指纹 + 浏览器 UA，
  普通播放器 403，所以只能中转。SB、VO 能 302。AV 没做。
- **JAVMost**：取嵌入页走 POST 接口（路径从页面脚本里取），服务器编号对应固定的播放站；分成多段的服务器跳过。
  Dood 不认 JAVMost 当 Referer（"Video embed restricted"），不带 Referer 反而放行，取嵌入页时遇到这种情况自动重试。
  TurboVid、PlayerSB 在本机代理下连不上，没实测。
- **nJAV**：njavtv.com 是 MissAV 镜像，加进 MissAV 默认域名；njav.tv 已跳到 123av（单线路、只能中转），没接。
- **实测**（本机）：JavGuru SB / VO 302，JK / LU / DD 中转；JAVMost DOO 302、DOOD 中转；SupJav AARM-370 的
  ST / EVS / VOE 302、VAS / LUC 中转，都能被 ffprobe 读出。中转一个 1.7 GB 的 mp4 时中途断开，服务内存稳定在 73 MB。
