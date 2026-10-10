import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { useState } from "react";
import { Chip, Notice, Panel } from "@/components/ui/data";
import { Field, Select } from "@/components/ui/form";
import { api, type SettingsResponse } from "@/lib/api";
import { PLAY_MODES, RESOLVE_MODES } from "@/lib/labels";

const examples = [
  { title: "自动直连，需要时由网关中转", mode: "auto", link: "relay", detail: "只统计经过网关的中转流量；hls2strm 不必对公网开放。" },
  { title: "全部经网关中转，统一统计下行", mode: "proxy", link: "relay", detail: "所有成功命中此解析后端的媒体都经过网关，同时占用两层服务的带宽。" },
  { title: "优先直连，失败允许网关中转", mode: "redirect", link: "relay", detail: "先找外部可直连的源；没有或解析失败时，允许返回中转地址。" },
  { title: "需要中转时直接访问 hls2strm", mode: "auto", link: "redirect", detail: "必须配置可公网访问的 resolve_proxy_url；中转媒体不计入网关流量。" },
  { title: "解析接口只返回直链", mode: "strict_redirect", link: "relay", detail: "无法直连时解析报错。网关仍可能切备用后端或回退 Emby，这不是全链路禁止中转。" },
];

function Path({ nodes }: { nodes: string[] }) {
  return (
    <ol className="flex flex-wrap items-center gap-2 text-sm" aria-label="媒体数据流向">
      {nodes.map((node, index) => (
        <li key={`${index}-${node}`} className="flex items-center gap-2">
          {index > 0 && <ArrowRight className="size-4 shrink-0 text-muted" aria-hidden="true" />}
          <span className="rounded-md border border-line bg-panel-2 px-3 py-2">{node}</span>
        </li>
      ))}
    </ol>
  );
}

export default function PlaybackGuide() {
  const { data, isError } = useQuery({ queryKey: ["settings"], queryFn: ({ signal }) => api.get<SettingsResponse>("/api/settings", signal), staleTime: Infinity });
  const [selectedMode, setMode] = useState("");
  const [link, setLink] = useState("relay");
  const savedMode = String(data?.values.resolve_mode ?? "auto");
  const mode = selectedMode || savedMode;
  const relayPath = link === "relay" ? ["源站 CDN", "hls2strm", "Emby Gateway", "播放器"] : ["源站 CDN", "hls2strm", "播放器"];

  return (
    <div className="space-y-4">
      <Notice>
        播放路径由两层配置共同决定：hls2strm 决定是否需要中转，网关决定需要中转时由谁转发。
        这里是只读说明与路径演示，不保存任何配置。
      </Notice>

      <Panel title="先分清三个开关" bodyClassName="space-y-3">
        <p className="text-sm"><b>普通播放模式 play_mode：</b>控制直接访问 hls2strm /play/… 的播放；direct 还会让新生成的 STRM 直接写 CDN 地址。</p>
        <p className="text-sm"><b>网关解析策略 resolve_mode：</b>控制 /api/resolve 返回直链还是中转地址，与普通播放模式独立。</p>
        <p className="text-sm"><b>网关链路模式：</b>只对解析结果中需要中转的资源生效。Relay 让媒体经过网关，302 让播放器直接访问解析服务的公网中转地址。</p>
        <p className="text-sm text-muted">故障回退、Emby 自己拉流、播放器直接打开 STRM 都是运行路径，不是还需要配置的其他链路模式。</p>
        <a className="inline-block text-sm text-accent underline" href="#/settings">前往设置修改并保存</a>
      </Panel>

      <Panel title="配置组合与媒体路径" bodyClassName="space-y-4">
        {data ? (
          <div className="flex flex-wrap gap-2 text-sm">
            <Chip>已保存的普通播放：{PLAY_MODES[String(data.values.play_mode)] ?? String(data.values.play_mode)}</Chip>
            <Chip>已保存的网关策略：{RESOLVE_MODES[savedMode] ?? savedMode}</Chip>
          </div>
        ) : <p className="text-sm text-muted">{isError ? "当前配置读取失败，仍可查看说明与示例。" : "正在读取已保存配置…"}</p>}
        <p className="text-sm text-muted">网关配置不在本服务中，下面的网关选项仅用于演示，请按你的实际网关配置选择。</p>
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="演示 hls2strm 网关解析策略">
            <Select value={mode} onChange={event => setMode(event.target.value)}>
              {Object.entries(RESOLVE_MODES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </Select>
          </Field>
          <Field label="演示网关链路模式（非实际配置）">
            <Select value={link} onChange={event => setLink(event.target.value)}>
              <option value="relay">网关中转（Relay）</option>
              <option value="redirect">302 到解析服务的公网中转地址</option>
            </Select>
          </Field>
        </div>
        {mode !== "proxy" && (
          <div className="space-y-2 rounded-md border border-line p-3">
            <b className="text-sm">允许客户端直连时</b>
            <Path nodes={["源站 CDN", "播放器"]} />
            <p className="text-sm text-muted">网关发放 302 后，视频字节不经过网关。{mode === "redirect" || mode === "strict_redirect" ? "优先寻找外部可直连的源。" : "按画质、站点与线路偏好选源，能直连的直接交给播放器。"}</p>
          </div>
        )}
        {mode === "strict_redirect" ? (
          <Notice>需要中转时，解析接口返回错误。网关按自己的资源池与回退逻辑继续处理；不能据此保证整条链路从不经过 Emby 或其他中转。</Notice>
        ) : (
          <div className="space-y-2 rounded-md border border-line p-3">
            <b className="text-sm">{mode === "proxy" ? "所有资源强制中转" : "所选源或客户端需要中转时"}</b>
            <Path nodes={relayPath} />
            <p className="text-sm text-muted">{link === "relay" ? "会先 302 到网关自身的令牌地址，HLS 清单、分片或 MP4 数据随后经过网关，计入网关下行。resolve_proxy_url 可以留空。" : "会 302 到 hls2strm 公网中转地址，后续视频字节不经过网关。resolve_proxy_url 必须填写可外网访问的地址。"}</p>
          </div>
        )}
        <p className="text-xs text-muted">以上假设解析接口鉴权成功、网关路由命中且有可用源；接口报错和后端切换不属于成功路径。</p>
      </Panel>

      <Panel title="按目标选择组合" bodyClassName="grid gap-3 md:grid-cols-2">
        {examples.map(example => (
          <button key={example.title} type="button" onClick={() => { setMode(example.mode); setLink(example.link); }}
            className="space-y-2 rounded-md border border-line p-3 text-left hover:bg-panel-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
            <b className="block text-sm">{example.title}</b>
            <code className="block text-xs text-accent">resolve_mode={example.mode} · 网关={example.link}</code>
            <span className="block text-sm text-muted">{example.detail}</span>
            <span className="block text-xs text-muted">点击仅切换上方演示</span>
          </button>
        ))}
      </Panel>

      <Panel title="回退和绕过网关的路径" bodyClassName="space-y-4">
        <div className="space-y-2 text-sm">
          <b>解析失败或路由未命中</b>
          <p className="text-muted">播放拦截可切备用后端或反代 Emby。Emby 自己拉流并输出视频时，数据仍经过网关；Emby 如果把 STRM 地址 302 给播放器，后续数据是否经过网关取决于该地址。内网 STRM 地址直接交给外网播放器可能无法播放。</p>
          <Path nodes={["源站 CDN", "hls2strm", "Emby", "Emby Gateway", "播放器"]} />
          <p className="text-xs text-muted">上图是 Emby 自己拉流的一种情况；直接请求普通 /stream/… 且所有后端失败时，网关返回 502。</p>
        </div>
        <div className="space-y-2 text-sm">
          <b>网页试播、直接打开 hls2strm 链接</b>
          <Path nodes={["源站 CDN", "hls2strm", "播放器"]} />
          <p className="text-muted">普通播放允许直连时也可能直接 CDN → 播放器。上述两种都不进入网关计量。play_mode=direct 写入的 CDN 地址也绕过正常解析入口。</p>
        </div>
      </Panel>

      <Panel title="地址、令牌和统计各归谁负责" bodyClassName="space-y-3 text-sm">
        <p><b>Emby：</b>账号和媒体权限的权威来源，接收客户端播放状态上报。</p>
        <p><b>hls2strm：</b>选源、解析和刷新 CDN 地址，保存源与线路的播放会话，按需中转。resolve_token 保护解析接口；play_token 保护本服务的播放入口。</p>
        <p><b>网关中转票据：</b>在后续分片不再携带 Emby 凭证时恢复已验证的用户、Source 和后端，继续做访问检查并归属流量；它不是 CDN 地址缓存。</p>
        <p className="text-muted">现有网关中转票据存于内存，重启后旧链接需重新发起播放。resolve_relay_ttl 目前还被网关用于票据期限；设为 0 不会关闭票据，旧网关会使用 6 小时默认期限。</p>
        <p><b>下行统计：</b>只包含实际经过网关的响应体字节，不含直连 CDN 流量及 TCP/TLS 开销。请求结束后结算，长 MP4 请求完成前不会持续入账；302 到网关自身也算一次重定向。</p>
      </Panel>

      <Panel title="接入检查" bodyClassName="space-y-2 text-sm">
        <p>1. 在两端设置一致的解析令牌。hls2strm 的 resolve_token 留空时，解析接口返回 403；播放正常也可能只是走了 Emby 回退。</p>
        <p>2. 网关解析超时应大于 hls2strm 的 resolve_timeout。默认 20 秒解析预算时，建议网关 25～30 秒；自定义了预算则相应调整。</p>
        <p>3. Relay 模式下 resolve_proxy_url 可留空。public_base_url 用于写 STRM，留空时取环境变量；应保证 Emby 能访问生效的地址。</p>
        <p>4. 在网关日志看初始 Location，再看后续 /stream/http_resolver/… 的 200/206 和 bytes_out；不要仅凭 302 次数判断是否直连。</p>
      </Panel>
    </div>
  );
}
