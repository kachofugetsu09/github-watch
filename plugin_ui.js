// 侧栏插件面板里的 GitHub Watch 用量：仓库 -> Issue/PR -> 逐轮。不经构建，直接用宿主共享的 React。
import { createElement as h, Fragment, useEffect, useState } from "react";
import { render } from "@akashic/chat-ui-v1";

const ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="12.5" r="1.5"/><path d="M4.5 5v6M11.5 11V7.5a2 2 0 0 0-2-2H7"/><path d="M8.5 4 7 5.5 8.5 7"/></svg>';
const TRIGGERS = { opened: "新建", ready_for_review: "转为待审", owner_mention: "被提及" };

const fmt = (value) => value >= 1e6 ? `${(value / 1e6).toFixed(1)}M` : value >= 1e3 ? `${Math.round(value / 1e3)}k` : String(value);
const pct = (value) => value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
const kind = (value) => value === "pr" ? "PR" : "Issue";
const when = (value) => new Date(value).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });

// 同一 query 与参数只发一次；面板重新挂载时重读。
function useQuery(query, method, payload) {
  const key = JSON.stringify(payload);
  const [state, setState] = useState({ data: null, error: "" });
  useEffect(() => {
    let live = true;
    setState({ data: null, error: "" });
    query(method, payload).then((data) => live && setState({ data, error: "" }),
      (error) => live && setState({ data: null, error: error instanceof Error ? error.message : String(error) }));
    return () => { live = false; };
  }, [query, method, key]);
  return state;
}

function Metric({ value, label }) {
  return h("div", { className: "gw-metric" }, h("b", null, value), h("span", null, label));
}

function Bars({ turns }) {
  const peak = Math.max(1, ...turns.map((turn) => turn.input_tokens));
  return h("div", { className: "gw-bars", role: "img", "aria-label": "逐轮输入，深色未命中、浅色命中缓存" },
    turns.map((turn, index) => h("span", { key: index, title: `${when(turn.recorded_at)} · ${fmt(turn.input_tokens)} · 命中 ${pct(turn.input_tokens ? turn.cached_input_tokens / turn.input_tokens : null)}` },
      h("i", { style: { height: `${(turn.input_tokens - turn.cached_input_tokens) / peak * 100}%` } }),
      h("i", { className: "soft", style: { height: `${turn.cached_input_tokens / peak * 100}%` } }))));
}

function Detail({ query, repo, item }) {
  const { data, error } = useQuery(query, "usage.turns", { repo, number: item.number });
  return h("div", { className: "gw-detail" },
    h("div", null, h("div", { className: "gw-title" }, `${repo} #${item.number}`),
      h("div", { className: "gw-note" }, `${kind(item.kind)} · 最近 ${when(item.last_recorded_at)}`)),
    h("div", { className: "gw-kpis" },
      h(Metric, { value: fmt(item.input_tokens), label: `输入 · 输出 ${fmt(item.output_tokens)}` }),
      h(Metric, { value: pct(item.hit_rate), label: "缓存命中" }),
      h(Metric, { value: String(item.turns), label: `轮 · ${item.calls} 次调用` })),
    !data ? h("p", { className: "gw-note" }, error || "正在读取…") : h(Fragment, null,
      h(Bars, { turns: data.turns }),
      h("div", { className: "gw-list" }, data.turns.slice().reverse().map((turn) => h("div", { key: turn.recorded_at, className: "gw-item" },
        h("span", { className: "t" }, TRIGGERS[turn.trigger_kind] || turn.trigger_kind),
        h("span", { className: "n" }, fmt(turn.input_tokens)),
        h("span", { className: "s" }, `${when(turn.recorded_at)} · ${turn.models.join("、") || "未知模型"}${turn.excluded_calls ? ` · ${turn.excluded_calls} 次未计` : ""}`),
        h("span", { className: "n" }, pct(turn.input_tokens ? turn.cached_input_tokens / turn.input_tokens : null)))))),
    h("p", { className: "gw-note" }, "同一 Issue/PR 复用一个会话，后面的轮次带着完整历史，输入是累积值。"));
}

function Item({ repo, item, selected, pick }) {
  return h("button", { type: "button", className: "gw-item",
    "aria-current": selected && selected.repo === repo && selected.number === item.number || undefined,
    onClick: () => pick({ repo, number: item.number, item }) },
    h("span", { className: "t" }, `#${item.number} ${kind(item.kind)}`), h("span", { className: "n" }, fmt(item.input_tokens)),
    h("span", { className: "s" }, `${item.turns} 轮 · ${when(item.last_recorded_at)}`), h("span", { className: "n" }, pct(item.hit_rate)));
}

// 首屏每个仓库只列前几个对象；点「列出全部」再按仓库读完整列表，回包不随仓库数膨胀。
function RepoObjects({ query, repo, selected, pick }) {
  const [full, setFull] = useState(null);
  const [error, setError] = useState("");
  const objects = full ? full.objects : repo.objects;
  const more = full ? full.more : repo.more;
  const load = () => query("usage.objects", { repo: repo.repo }).then(setFull,
    (reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  return h(Fragment, null,
    objects.map((item) => h(Item, { key: item.number, repo: repo.repo, item, selected, pick })),
    !more ? null : full ? h("p", { className: "gw-note" }, `另有 ${more} 个对象未列出`)
      : h("button", { type: "button", className: "gw-more", onClick: load }, error || `列出全部（另有 ${more} 个）`));
}

function Repos({ query, data, selected, pick }) {
  return h("div", { className: "gw-list" }, data.repos.map((repo) => h(Fragment, { key: repo.repo },
    h("div", { className: "gw-repo" }, h("span", { className: "t" }, repo.repo),
      h("span", { className: "n" }, fmt(repo.totals.input_tokens)), h("span", { className: "n" }, pct(repo.totals.hit_rate))),
    h(RepoObjects, { query, repo, selected, pick }))),
    data.more ? h("p", { className: "gw-note" }, `另有 ${data.more} 个仓库未列出`) : null);
}

function Panel({ context }) {
  const { panel } = context;
  const [size, setSize] = useState(panel.size());
  const [selected, setSelected] = useState(null);
  const [pushed, setPushed] = useState(false);
  const { data, error } = useQuery(context.query, "usage.repos", {});
  useEffect(() => context.onInvalidate(() => setSize(panel.size())), [context, panel]);
  if (!data) return h("p", { className: "gw-note gw-pad" }, error || "正在读取…");
  if (!data.repos.length) return h("p", { className: "gw-note gw-pad" }, "还没有结算的用量。Issue/PR 的一轮工作结束后才计入。");
  const totals = data.totals;
  const kpis = h("div", { className: "gw-kpis" },
    h(Metric, { value: fmt(totals.input_tokens), label: `输入 · 输出 ${fmt(totals.output_tokens)}` }),
    h(Metric, { value: pct(totals.hit_rate), label: "缓存命中" }),
    h(Metric, { value: String(totals.objects), label: `个对象 · ${totals.turns} 轮` }));
  const pick = (next) => { setSelected(next); setPushed(true); if (size === "compact") panel.setSize("expanded"); };
  if (size === "compact") return h("div", { className: "gw-panel" }, kpis, h(Repos, { query: context.query, data, selected: null, pick }));
  const active = selected || { repo: data.repos[0].repo, number: data.repos[0].objects[0].number };
  const item = active.item || data.repos.find((repo) => repo.repo === active.repo)?.objects.find((entry) => entry.number === active.number);
  const detail = item ? h(Detail, { key: `${active.repo}#${active.number}`, query: context.query, repo: active.repo, item }) : null;
  const list = h("div", { className: "gw-pane" }, kpis, h(Repos, { query: context.query, data, selected: active, pick }));
  if (panel.sheet) return h("div", { className: "gw-panel" }, pushed && detail
    ? h(Fragment, null, h("button", { type: "button", className: "gw-back", onClick: () => setPushed(false) }, "← 返回列表"), detail) : list);
  return h("div", { className: "gw-panel gw-split" }, list, h("div", { className: "gw-pane" }, detail));
}

export default {
  slots: {
    "sidebar.panels": {
      label: "GitHub 用量",
      iconSvg: ICON,
      mount: (host, context) => render(host, h(Panel, { context })),
    },
  },
};
