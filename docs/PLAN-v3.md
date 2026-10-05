# v3 迭代方案：输出库、订阅、strm 扫描与改前缀、网关集成

> 2026-10-06。已确认的决定：
> - 分目录两种方式都做：先做"按任务 + 订阅"，再做"规则库"
> - 扫描要识别三类 strm：本服务生成的、其他 Jable 工具生成的、其他来源的
> - 和 embyGateway 用原生后端的方式结合

## 1. 输出库 + 订阅

### 数据模型

| 表 | 字段 | 说明 |
|---|---|---|
| `libraries` | id, name, dir, path_template, rule, created_at | `dir` 可以是相对输出根目录的路径，也可以是绝对路径；`path_template` 为空时用全局设置；`rule` 留给第 4 节的规则库 |
| `outputs` | video_id, library_id, strm_path, cover_done, via, written_at（主键：video_id + library_id） | 影片和库是多对多关系。`via` 记录来源，取值 `job`、`rule`、`adopt` |
| `subscriptions` | id, name, source, sort, library_id, detail, interval, stop_after_known, max_pages, enabled, initialized, last_run_at | 定时任务 |
| `videos` | 去掉 strm_path、cover_done、output_at | 这三项迁到 `outputs` |

- **库目录不能互相嵌套**：保存库时校验，避免 Emby 指向父目录时把子库重复扫进来。
- **迁移机制**：用 `PRAGMA user_version` 做版本化迁移。从 v1 升到 v2 时：
  1. 建默认库"全部"，目录为 `全部`；
  2. 原来的 `videos.strm_path` 转成 `outputs` 记录；
  3. 原来的全局增量设置转成默认订阅"最新更新 → 全部"；
  4. 跑一次重写，把文件从 `{根}/{slug}/` 搬到 `{根}/全部/{slug}/`。

### 行为

- 建任务时选输出库，默认是"全部"。列表任务和它派生出的详情任务都写到这个库。
- 详情更新会改动共享的元数据，所以该影片所在的每个库里的 nfo 都要重写。
- 同一部影片在多个库里时，封面优先用硬链接，同一文件系统下不额外占空间；跨文件系统时才复制。
- **订阅**：
  - 首次运行抓全部页（`initialized` 为假时），之后每次按增量规则跑：连续遇到已入库影片就停，或者达到页数上限。
  - "开始全站抓取"按钮改为触发默认订阅的首轮全量。
  - 例子：`/categories/chinese-subtitle/` → 库"中文字幕"，每 60 分钟跑一次。
- **页面**：
  - 新增"输出库与订阅"页：增删改库，查看每个库的影片数；增删改订阅，可立即运行，能看到上次运行的任务；
  - 任务页增加"输出库"下拉框；
  - 影片详情弹窗列出这部影片所在的库和对应文件。
- **删除库**：可以选择是否连同文件一起删除。

## 2. 扫描已有 strm、纳管、批量改前缀

### 扫描

- 后台任务，参数是一个目录（默认输出根目录）。遍历其中的 `*.strm`，结果写入 `strm_files` 表，字段有：path、scan_id、mtime、url、prefix（`scheme://host:port`）、kind、slug、video_id、library_id。
- `kind` 的识别规则：

| kind | 判定方式 | 能拿到什么 |
|---|---|---|
| `ours` | URL 路径匹配 `…/play/{slug}.m3u8`，域名不限 | slug |
| `cdn` | 域名是 `*.mushroomtrack.com`，路径为 `/hls/{token}/{expires}/{n}/{videoId}/{videoId}.m3u8` | videoId，以及是否已过期 |
| `named` | URL 不认识，但文件名或父目录里有番号 | 推测出的 slug，需要抓详情确认 |
| `other` | 合法 URL 但来源不明（115、alist 等） | 只参与前缀统计和改前缀 |
| `invalid` | 空文件或不是 URL | 只统计 |

- 结果页：按 kind 和前缀分组计数，可以按目录、kind、前缀筛选。还会列出两类孤儿：`outputs` 里有记录但文件已不存在的；`ours` 类文件对应的 slug 不在库里的。

### 纳管

- 适用于 `ours`、`cdn`、`named` 三类。
- 影片已在库里：给它建一条 `outputs` 记录（`via=adopt`，库按所在目录推断，推断不出就手动选）。`cdn` 和 `named` 两类还会把文件内容改写成本服务格式（CDN 直链 3 小时就过期）。
- 影片不在库里：已知 slug 的，排队抓详情，确认存在后再纳管。
- 只有 videoId、文件名里又没有番号的 `cdn` 文件：站点目前没有已知的"videoId 查 slug"途径，先标记为无法识别，实现时再试一下 KVS 是否支持按 id 访问。

### 改前缀和回滚

- 先预览：给出命中的文件数，以及 20 条改前改后的对照。
- 确认后作为后台任务执行：写之前重新读一遍文件，确认内容仍以旧前缀开头，然后原子写入；每个文件的改动记入 `strm_changes`（change_set_id, path, old, new）。
- 回滚：按改动批次恢复。只恢复当前内容仍等于"新内容"的文件，中途被别处改过的跳过。
- 对所有来源的 strm 都有效。旧前缀等于本服务的"对外地址"时，设置会同步更新。
- 可选：设置里配置 Emby/Jellyfin 的地址和 API Key，改前缀或重写输出后自动触发媒体库刷新（放在最后实施）。

## 3. resolve 接口 + 浏览器兼容（本服务）

- 新增 `GET /api/resolve/{slug}.m3u8`，用新增设置 `resolve_token` 做 Bearer 认证。
  - 参数：`ua`、`origin`（网关转来的客户端信息）、`min_remaining`。
  - 返回 200 + JSON：`{"url", "expires_at", "ttl", "duration"}`。
  - 客户端不能直连 CDN 时（UA 命中中转列表，或者带了 `origin`）返回 409 + `{"reason"}`，网关据此回退为反代 Emby。
  - 已下架返回 404；被拦截返回 503 并带 `Retry-After`。
- `/play` 的浏览器兼容：请求带 `Origin` 头时改走中转，`/play` 和 `/hls` 都加上 CORS 头，并处理 `OPTIONS` 预检。用来兼容 Emby Web 直接读 strm 地址的情况。

## 4. 规则库（第 1 节完成之后做）

- 库的 `rule` 字段：`{"categories": [], "tags": [], "models": [], "quality": [], "keyword": "", "match": "any|all"}`。
- 触发时机：
  - 详情入库后，对所有规则库求值；命中就加 `outputs`（`via=rule`），不再命中就删掉 `via=rule` 的输出（连同文件）；
  - "重新归库"任务：只在本地对全库重新求值，不联网。
- 依赖详情数据：还没抓详情的影片不会命中任何规则。

## 5. embyGateway 新增 `http_resolver` 后端（在网关仓库里改）

- **命名**：建议做成通用的"外部解析后端" `http_resolver`，而不是专门叫 `jable_strm`。理由是协议很简单：objectKey 发过去，换回一个 URL 和 TTL，以后别的站点也能复用。
- **协议**：`GET {api_url}/{objectKey}?ua=&origin=`，请求头带 `Authorization: Bearer {token}`。返回 200 时读取 JSON 里的 `url` 和 `ttl` 去做 302；其他状态码都当作失败，交给备用后端，或者按网关现有逻辑回退为反代 Emby。
- **需要改动的网关文件**：

| 文件 | 改动 |
|---|---|
| `config_v2.go` | 新增 `HTTPResolverBackendConfig{APIURL, Token, TimeoutSeconds}`；`BackendConfig` 加对应字段；补全类型注释 |
| `v2_http_resolver_backend.go`（新文件） | 适配器实现 |
| `v2_backends.go` | 加一个 `case "http_resolver"` |
| `v2_source_server.go` | 构造 ctx 时带上客户端的 UA 和 Origin（新 helper），`tryResolvePlaybackRoute` 和 `handleDirectStream` 都用它 |
| `store/postgres_cfg_schema.go` | 新表 `cfg_backend_http_resolver` |
| `v2_config_relational_db.go` | 读写这张新表；SQLite 快照模式走 JSON，不需要改 |
| license | 新特性 `backend.http_resolver`，需要你的许可证服务签发，否则这个后端会被特性开关禁用 |
| `webui`（`types.ts`、`useBackendsPage.ts`、`BackendListSection.vue`） | 后端表单加这一类型 |

- **网关里的配置**（你在管理台操作）：
  1. 新建 `http_resolver` 后端，`api_url` 填 `http://jable-strm:8080/api/resolve`；
  2. 新建资源池，主后端选它；
  3. 在 Emby Source 上加路由：正则 `^/play/[^/]+\.m3u8$`，路径规则集为空映射，objectKey 就是 `play/ipzz-983.m3u8`。适配器会取最后一段，或者设一条映射 `/play` → `/`。
- **遵守网关仓库的 AGENTS.md**：不运行代码，不写测试，不写文档。编译和验证由你来做。
- **部署后要确认**：Emby 客户端播放 strm 里的 HLS 时，请求的是 `stream.m3u8`（会被拦截）还是 `master.m3u8`（不会被拦截）。看网关的请求日志就能知道。

## 6. 实施顺序

每一步单独提交，并带上测试（只限本仓库）：

1. DB 版本化迁移 + 输出库 + 订阅（含页面）
2. 扫描 + 纳管 + 改前缀 + 回滚（含页面）
3. resolve 接口 + 浏览器 CORS
4. 规则库
5. 网关 `http_resolver` 后端（网关仓库）
6. 可选：Emby/Jellyfin 媒体库刷新联动
