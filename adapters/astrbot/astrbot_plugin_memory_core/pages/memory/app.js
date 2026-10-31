/* memory-core dashboard (read-only). Every piece of memory text is put in with textContent: it came from chats. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

  const MEMORY_KIND = {
    fact: "事实", event: "事件", relation: "关系", reflection: "反思", diary: "日记", preference: "偏好",
    lexicon: "黑话", style: "说话方式",
  };
  const ENTITY_KIND = { person: "人", group: "群", place: "地点", work: "作品", concept: "概念", thing: "物品" };
  const STATUS = { active: "有效", archived: "归档", superseded: "已替换", forgotten: "已遗忘" };
  const CHANNEL = { text: "全文", vector: "向量", entity: "提到的人和事" };
  const PART = { relevance: "相关度", recency: "新近", importance: "重要性", strength: "强度" };
  const PLATFORM = { webchat: "网页对话", telegram: "Telegram", aiocqhttp: "QQ", qq: "QQ", web: "网上" };
  const NEW_GROUP = "panel:group:new";

  const state = {
    owner: "主人", self: "她", ownerSpaces: [], spaces: [], loaded: {},
    mem: { offset: 0, limit: 30, total: 0, filter: {}, selected: null, entityName: "" },
    graph: { view: "entities", data: null, chart: null },
  };

  // ------------------------------------------------------------------------------------------ helpers

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else if (k === "style") node.setAttribute("style", v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function clear(node, ...children) {
    node.replaceChildren(...children.flat(Infinity).filter(Boolean));
    return node;
  }

  function showError(message) {
    const box = $("error");
    if (!message) { box.hidden = true; return; }
    box.textContent = message;
    box.hidden = false;
  }

  async function api(method, endpoint, payload) {
    if (!bridge) throw new Error("请在 AstrBot 后台的「插件 → memory-core → 视图」里打开这个页面");
    try {
      const data = method === "GET"
        ? await bridge.apiGet("page/" + endpoint, payload || {})
        : await bridge.apiPost("page/" + endpoint, payload || {});
      showError("");
      return data;
    } catch (err) {
      showError("出错了：" + (err && err.message ? err.message : err));
      throw err;
    }
  }

  function time(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const pad = (n) => String(n).padStart(2, "0");
    const year = d.getFullYear() !== new Date().getFullYear() ? d.getFullYear() + "-" : "";
    return `${year}${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function spaceLabel(space) {
    if (!space) return "";
    if (space === NEW_GROUP) return "一个新的群";
    const [platform, type, ...rest] = space.split(":");
    const where = PLATFORM[platform] || platform;
    if (state.ownerSpaces.includes(space)) return `${state.owner}私聊（${where}）`;
    if (type === "group") return `${where} 群 ${rest.join(":")}`;
    if (type === "private") return `${where} 私聊 ${rest.join(":")}`;
    return type ? `${where} ${type} ${rest.join(":")}`.trim() : where;
  }

  function scopeLabel(scope) {
    if (scope === "public") return "公开";
    if (scope === "self") return `${state.self}自己的`;
    if (scope === "owner") return `只给${state.owner}`;
    if (scope && scope.startsWith("space:")) return "只在" + spaceLabel(scope.slice(6));
    return scope || "";
  }

  function scopeBadge(scope) {
    const cls = scope === "owner" ? "scope-owner" : scope === "self" ? "scope-self" : "";
    return el("span", { class: "badge " + cls, title: scope }, scopeLabel(scope));
  }

  function badge(text, cls, title) {
    return el("span", { class: "badge " + (cls || ""), title }, text);
  }

  function num(x) {
    return typeof x === "number" ? x.toFixed(2).replace(/0$/, "").replace(/\.0?$/, "") : "";
  }

  function table(headers, rows) {
    return el("table", {},
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", { class: h.num ? "num" : "" }, h.label || h)))),
      el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { class: headers[i].num ? "num" : "" }, c))))));
  }

  // ------------------------------------------------------------------------------------------ pages

  const pages = ["overview", "memories", "recall", "graph"];

  function show(page) {
    for (const p of pages) {
      $("page-" + p).hidden = p !== page;
      const tab = document.querySelector(`.tab[data-page="${p}"]`);
      if (p === page) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
    }
    if (state.graph.chart) state.graph.chart.setVisible(page === "graph");
    if (!state.loaded[page]) {
      state.loaded[page] = true;
      ({ overview: loadOverview, memories: loadMemories, recall: setupRecall, graph: loadGraph })[page]().catch(() => {
        state.loaded[page] = false;
      });
    }
  }

  // ------------------------------------------------------------------------------------------ overview

  async function loadOverview() {
    const d = await api("GET", "overview");
    state.owner = d.owner_title || state.owner;
    state.self = d.self_name || state.self;
    state.ownerSpaces = d.owner_spaces || [];
    state.spaces = (d.spaces || []).map((s) => s.space);
    $("recall-owner-label").textContent = `${state.owner}在场`;
    $("ov-abilities-title").textContent = `${state.self}会什么`;
    const pending = Object.values(d.pending || {}).reduce((a, b) => a + b, 0);
    clear($("ov-cards"),
      card(d.counts.episodes, "条经历"),
      card(d.counts.memories, "条记忆"),
      card(d.counts.entities, "个人和事"),
      card(d.counts.embeddings, d.embedder ? `个向量（${d.embedder}）` : "个向量（没有嵌入模型，只用全文和实体回忆）"),
      card(pending, "条经历等着整理"));

    const kinds = {};
    for (const r of d.memories) (kinds[r.kind] = kinds[r.kind] || {})[r.status] = r.count;
    const cols = ["active", "archived", "superseded", "forgotten"];
    clear($("ov-kinds"), table(
      ["类型", ...cols.map((c) => ({ label: STATUS[c], num: true }))],
      Object.keys(kinds).sort().map((k) => [MEMORY_KIND[k] || k, ...cols.map((c) => kinds[k][c] || "")])));

    clear($("ov-scopes"), table(["范围", { label: "条数", num: true }],
      Object.entries(d.active_by_scope).map(([s, n]) => [scopeBadge(s), n])));

    clear($("ov-spaces"), table(["地方", { label: "经历", num: true }, "最近", { label: "待整理", num: true }],
      (d.spaces || []).map((s) => [el("span", { title: s.space }, spaceLabel(s.space)), s.episodes, time(s.last),
        (d.pending || {})[s.space] || ""])));

    clear($("ov-abilities"), d.abilities && d.abilities.length
      ? el("ul", {}, d.abilities.map((a) => el("li", {}, el("b", {}, a.name), "：", a.text,
        el("span", { class: "muted small" }, "  ", a.key))))
      : el("div", { class: "muted" }, d.abilities_error ? "读不到：" + d.abilities_error : "（空）"));
    $("ov-now").textContent = d.inner_state || d.inner_state_error || "（没有开启内在状态）";
  }

  function card(n, label) {
    return el("div", { class: "card" }, el("div", { class: "n" }, n), el("div", { class: "l" }, label));
  }

  // ------------------------------------------------------------------------------------------ memories

  function setupMemoryFilters() {
    const f = $("mem-filter");
    for (const [k, v] of Object.entries(MEMORY_KIND)) f.kind.append(el("option", { value: k }, v));
    for (const s of ["public", "self", "owner", "space:*"]) {
      f.scope.append(el("option", { value: s }, s === "space:*" ? "只在某个群或私聊" : scopeLabel(s)));
    }
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      state.mem.offset = 0;
      loadMemories();
    });
    for (const name of ["kind", "status", "scope"]) f[name].addEventListener("change", () => {
      state.mem.offset = 0;
      loadMemories();
    });
    $("mem-prev").addEventListener("click", () => {
      state.mem.offset = Math.max(0, state.mem.offset - state.mem.limit);
      loadMemories();
    });
    $("mem-next").addEventListener("click", () => {
      state.mem.offset += state.mem.limit;
      loadMemories();
    });
  }

  function filterByEntity(id, name) {
    state.mem.filter.entity = id;
    state.mem.entityName = name;
    state.mem.offset = 0;
    show("memories");
    loadMemories();
  }

  async function loadMemories() {
    if (!state.loaded.overview) { state.loaded.overview = true; await loadOverview().catch(() => {}); }
    const f = $("mem-filter");
    const body = {
      text: f.text.value, kinds: f.kind.value ? [f.kind.value] : [], statuses: f.status.value ? [f.status.value] : [],
      scope: f.scope.value, entity: state.mem.filter.entity || "", offset: state.mem.offset, limit: state.mem.limit,
    };
    const chip = $("mem-entity");
    if (body.entity) {
      clear(chip, "关于 " + state.mem.entityName, el("button", { type: "button", title: "不按这个筛选", on: { click: () => {
        state.mem.filter.entity = "";
        state.mem.offset = 0;
        loadMemories();
      } } }, "×"));
      chip.hidden = false;
    } else chip.hidden = true;
    const d = await api("POST", "memories", body);
    state.mem.total = d.total;
    $("mem-total").textContent = `共 ${d.total} 条`;
    const pages = Math.max(1, Math.ceil(d.total / d.limit));
    $("mem-page").textContent = `第 ${Math.floor(d.offset / d.limit) + 1} / ${pages} 页`;
    $("mem-prev").disabled = d.offset <= 0;
    $("mem-next").disabled = d.offset + d.limit >= d.total;
    clear($("mem-list"), d.items.length ? d.items.map(memoryItem) : el("div", { class: "muted" }, "没有符合条件的记忆"));
  }

  function memoryItem(m) {
    const item = el("div", {
      class: "item" + (m.status !== "active" ? " dim" : "") + (state.mem.selected === m.id ? " selected" : ""),
      tabindex: "0", role: "button", "data-id": m.id,
      on: {
        click: () => openMemory(m.id, $("mem-detail")),
        keydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openMemory(m.id, $("mem-detail")); } },
      },
    },
    el("div", { class: "meta" }, badge(MEMORY_KIND[m.kind] || m.kind), scopeBadge(m.scope),
      m.status !== "active" ? badge(STATUS[m.status] || m.status, "status-" + m.status) : null),
    el("div", { class: "text" }, m.text || "（已遗忘）"),
    el("div", { class: "meta" },
      `重要性 ${num(m.importance)}`, "·", `强度 ${num(m.effective_strength)}`, "·", `更新于 ${time(m.updated_at)}`,
      m.subjects.length ? ["·", m.subjects.map((s) => s.name).join("、")] : null));
    return item;
  }

  async function openMemory(id, box) {
    state.mem.selected = id;
    for (const it of document.querySelectorAll("#mem-list .item")) it.classList.toggle("selected", it.dataset.id === id);
    const d = await api("GET", "memory", { id });
    renderMemory(d, box);
  }

  function renderMemory(d, box) {
    const m = d.memory;
    const close = el("button", { class: "ghost close", type: "button", on: { click: () => {
      box.hidden = true;
      state.mem.selected = null;
      for (const it of document.querySelectorAll("#mem-list .item.selected")) it.classList.remove("selected");
    } } }, "关闭");
    const kv = (pairs) => el("dl", { class: "kv" }, pairs.filter((p) => p[1] !== "" && p[1] !== null && p[1] !== undefined)
      .map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    clear(box,
      close,
      el("div", { class: "meta" }, badge(MEMORY_KIND[m.kind] || m.kind), scopeBadge(m.scope),
        m.status !== "active" ? badge(STATUS[m.status] || m.status, "status-" + m.status) : null),
      el("p", { class: "text" }, m.text || "（已遗忘）"),
      kv([
        ["重要性", num(m.importance)],
        ["强度", `现在 ${num(m.effective_strength)}（上次想起或写下时 ${num(m.strength)}）`],
        ["上次想起", time(m.last_recalled_at) || "还没被想起过"],
        ["有效期", m.valid_from || m.valid_to ? `${time(m.valid_from) || "…"} → ${time(m.valid_to) || "现在"}` : ""],
        ["整理方式", m.derived_by || ""],
        ["写下", time(m.created_at)],
        ["更新", time(m.updated_at)],
        ["日记日期", m.meta.date || ""],
        ["编号", el("code", { class: "small" }, m.id)],
      ]),
      el("h3", {}, "相关的人和事"),
      m.subjects.length
        ? el("div", { class: "row" }, m.subjects.map((s) => el("span", { class: "chip" },
          `${s.name}（${ENTITY_KIND[s.kind] || s.kind}）`,
          el("button", { type: "button", title: "看关于它的记忆", on: { click: () => filterByEntity(s.id, s.name) } }, "筛选"))))
        : el("div", { class: "muted" }, "没有关联"),
      m.subjects.length ? el("div", { style: "margin-top:8px" }, el("button", { class: "ghost", type: "button",
        on: { click: () => locateInGraph(m.id) } }, "在图谱中定位")) : null,
      el("h3", {}, `来自哪几句话（${d.sources.length}）`),
      d.sources.length
        ? d.sources.map((ep) => el("div", { class: "episode" },
          el("div", { class: "meta" }, ep.actor || "", "·", spaceLabel(ep.space), "·", time(ep.occurred_at)),
          el("div", {}, ep.forgotten ? "（这句已被遗忘）" : ep.content)))
        : el("div", { class: "muted" }, "没有记来源（比如她自己记下的、合并出来的）"),
      d.history.length > 1 ? [
        el("h3", {}, `版本（${d.history.length}）`),
        d.history.map((v) => el("div", { class: "version" + (v.id === m.id ? " current" : "") },
          el("div", { class: "meta" }, time(v.created_at), "·", STATUS[v.status] || v.status, v.id === m.id ? "· 正在看" : null),
          v.id === m.id ? el("div", {}, v.text || "（已遗忘）")
            : el("button", { class: "link", type: "button", on: { click: () => openMemory(v.id, box) } }, v.text || "（已遗忘）"))),
      ] : null);
    box.hidden = false;
    box.scrollTop = 0;
  }

  // ------------------------------------------------------------------------------------------ recall

  async function setupRecall() {
    if (!state.loaded.overview) { state.loaded.overview = true; await loadOverview().catch(() => {}); }
    const f = $("recall-form");
    const options = () => [...state.spaces, NEW_GROUP].map((s) => el("option", { value: s }, spaceLabel(s)));
    clear(f.space, options());
    clear(f.space2, options());
    const ownerSpace = state.spaces.find((s) => state.ownerSpaces.includes(s));
    if (ownerSpace) f.space.value = ownerSpace;
    f.owner_present.checked = state.ownerSpaces.includes(f.space.value);
    f.space2.value = state.spaces.find((s) => s.split(":")[1] === "group") || NEW_GROUP;
  }

  function wireRecall() {
    const f = $("recall-form");
    f.space.addEventListener("change", () => { f.owner_present.checked = state.ownerSpaces.includes(f.space.value); });
    f.compare.addEventListener("change", () => { $("recall-space2-wrap").hidden = !f.compare.checked; });
    f.query.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); f.requestSubmit(); }
    });
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const query = f.query.value.trim();
      if (!query) return;
      const out = $("recall-out");
      clear(out, el("div", { class: "muted" }, "想着…"));
      const ask = (space, owner) => api("POST", "recall", {
        query, space, owner_present: owner, include_archived: f.include_archived.checked, k: 10,
      });
      try {
        const runs = [ask(f.space.value, f.owner_present.checked)];
        if (f.compare.checked) runs.push(ask(f.space2.value, state.ownerSpaces.includes(f.space2.value)));
        const results = await Promise.all(runs);
        out.classList.toggle("two", results.length > 1);
        clear(out, results.map(recallColumn));
      } catch (_) {
        clear(out);
      }
    });
  }

  function recallColumn(r) {
    const max = Math.max(0.0001, ...r.hits.map((h) => h.score));
    const who = r.audience.owner_present ? `${state.owner}在场` : `${state.owner}不在场`;
    return el("div", {},
      el("div", { class: "panel" },
        el("div", {}, el("b", {}, spaceLabel(r.audience.space)), " · ", who),
        el("div", { class: "meta", style: "margin-top:6px" }, "能想起的范围：", r.scopes.map(scopeBadge)),
        r.mentioned.length ? el("div", { class: "meta", style: "margin-top:4px" }, "这句话提到了：", r.mentioned.join("、")) : null,
        r.vectors ? null : el("div", { class: "meta", style: "margin-top:4px" }, "没有嵌入模型：这次只用全文和实体"),
        el("div", { class: "meta", style: "margin-top:6px" }, Object.keys(PART).map((p) =>
          el("span", {}, el("span", { class: "dot p-" + p }), `${PART[p]} ×${r.weights[p]}`)))),
      r.hits.length ? r.hits.map((h, i) => {
        const m = h.memory;
        return el("div", { class: "hit" },
          el("div", { class: "meta" }, `#${i + 1}`, badge(MEMORY_KIND[m.kind] || m.kind), scopeBadge(m.scope),
            m.status !== "active" ? badge(STATUS[m.status], "status-" + m.status) : null,
            el("span", { style: "margin-left:auto" }, `总分 ${h.score.toFixed(3)}`)),
          el("div", { class: "text" }, m.text),
          el("div", { class: "bar", title: "总分的组成" }, Object.keys(PART).map((p) =>
            el("span", { class: "p-" + p, style: `width:${(100 * h.parts[p]) / max}%`, title: `${PART[p]} ${h.parts[p]}` }))),
          el("div", { class: "meta" },
            Object.keys(PART).map((p) => `${PART[p]} ${h.parts[p].toFixed(3)}`).join(" · "),
            el("span", { style: "margin-left:auto" }, "找到它的：",
              Object.entries(h.channels).map(([c, rank]) => `${CHANNEL[c] || c} 第${rank}`).join("，"))));
      }) : el("div", { class: "muted panel" }, "什么都没想起来"));
  }

  // ------------------------------------------------------------------------------------------ graph

  function wireGraph() {
    for (const b of document.querySelectorAll(".seg button[data-view]")) {
      b.addEventListener("click", () => {
        state.graph.view = b.dataset.view;
        for (const o of document.querySelectorAll(".seg button[data-view]")) o.setAttribute("aria-pressed", String(o === b));
        drawGraph();
      });
    }
    $("graph-status").addEventListener("change", loadGraph);
    $("graph-scope").addEventListener("change", loadGraph);
    $("graph-reset").addEventListener("click", () => state.graph.chart && state.graph.chart.relayout());
  }

  async function loadGraph() {
    if (!state.loaded.overview) { state.loaded.overview = true; await loadOverview().catch(() => {}); }
    const scopeSel = $("graph-scope");
    if (scopeSel.options.length === 1) {
      for (const s of ["public", "self", "owner", "space:*"]) {
        scopeSel.append(el("option", { value: s }, s === "space:*" ? "只在某个群或私聊" : scopeLabel(s)));
      }
    }
    if (!state.graph.chart) state.graph.chart = new window.MemGraph($("graph"), { onSelect: graphSelect });
    state.graph.data = await api("GET", "graph", { statuses: $("graph-status").value, scope: scopeSel.value });
    drawGraph();
  }

  function drawGraph() {
    const d = state.graph.data;
    if (!d) return;
    const chart = state.graph.chart;
    let nodes, links;
    const entities = d.entities.map((e) => ({ id: "e:" + e.id, ref: e.id, type: "entity", kind: e.kind, label: e.name, size: e.count }));
    if (state.graph.view === "entities") {
      nodes = entities;
      links = d.pairs.map((p) => ({ source: "e:" + p.a, target: "e:" + p.b, weight: p.weight }));
    } else {
      nodes = entities.concat(d.memories.map((m) => ({
        id: "m:" + m.id, ref: m.id, type: "memory", kind: m.kind, size: m.importance,
        label: m.text.length > 24 ? m.text.slice(0, 24) + "…" : m.text,
      })));
      links = d.links.map((l) => ({ source: "m:" + l.memory, target: "e:" + l.entity, weight: 1 }));
    }
    $("graph-empty").hidden = nodes.length > 0;
    chart.setData(nodes, links);
    const kinds = [...new Set(d.entities.map((e) => e.kind))];
    clear($("graph-legend"), kinds.map((k) => el("span", {}, el("span", { class: "dot", style: `background:var(--k-${k}, var(--k-thing))` }),
      ENTITY_KIND[k] || k)), state.graph.view === "full" ? el("span", {}, el("span", { class: "dot", style: "background:var(--k-memory)" }), "记忆") : null,
    el("span", { class: "muted" }, state.graph.view === "entities" ? "线越粗，一起出现的记忆越多" : "拖动、滚轮缩放、点一下看详情"));
    if (state.graph.pending) {
      const id = state.graph.pending;
      state.graph.pending = null;
      chart.select("m:" + id);
      graphSelect(chart.byId.get("m:" + id));
    }
  }

  async function graphSelect(node) {
    const side = $("graph-side");
    if (!node) { side.hidden = true; return; }
    if (node.type === "memory") {
      const d = await api("GET", "memory", { id: node.ref });
      renderMemory(d, side);
      return;
    }
    const d = await api("GET", "entity", { id: node.ref, statuses: $("graph-status").value });
    const e = d.entity;
    clear(side,
      el("button", { class: "ghost close", type: "button", on: { click: () => { side.hidden = true; state.graph.chart.select(null); } } }, "关闭"),
      el("h2", { style: "color:var(--text);font-size:16px" }, e.name),
      el("div", { class: "meta" }, badge(ENTITY_KIND[e.kind] || e.kind), `第一次出现 ${time(e.created_at)}`),
      e.aliases.length ? el("div", { class: "meta", style: "margin-top:6px" }, "也叫：", e.aliases.join("、")) : null,
      el("div", { class: "row", style: "margin-top:10px" },
        el("button", { class: "ghost", type: "button", on: { click: () => filterByEntity(e.id, e.name) } }, "在记忆页里看")),
      el("h3", {}, `${state.self}知道的（${d.memories.length}）`),
      d.memories.length ? d.memories.map((m) => el("div", { class: "version" },
        el("div", { class: "meta" }, badge(MEMORY_KIND[m.kind] || m.kind), scopeBadge(m.scope), `重要性 ${num(m.importance)}`),
        el("button", { class: "link", type: "button", style: "text-align:left", on: { click: async () => {
          renderMemory(await api("GET", "memory", { id: m.id }), side);
        } } }, m.text))) : el("div", { class: "muted" }, "没有"));
    side.hidden = false;
    side.scrollTop = 0;
  }

  function locateInGraph(memoryId) {
    state.graph.view = "full";
    for (const o of document.querySelectorAll(".seg button[data-view]")) o.setAttribute("aria-pressed", String(o.dataset.view === "full"));
    state.graph.pending = memoryId;
    if (state.loaded.graph && state.graph.data) {
      show("graph");
      if (!state.graph.data.memories.some((m) => m.id === memoryId)) {
        $("graph-status").value = "active,archived,superseded";
        loadGraph();
      } else drawGraph();
    } else {
      $("graph-status").value = "active,archived,superseded";
      show("graph");
    }
  }

  // ------------------------------------------------------------------------------------------ start

  function applyTheme(ctx) {
    if (ctx && typeof ctx.isDark === "boolean") {
      document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
      if (state.graph.chart) state.graph.chart.draw();
    }
  }

  for (const tab of document.querySelectorAll(".tab")) tab.addEventListener("click", () => show(tab.dataset.page));
  for (const b of document.querySelectorAll("[data-refresh]")) b.addEventListener("click", () => {
    const page = b.dataset.refresh;
    ({ overview: loadOverview })[page]();
  });
  setupMemoryFilters();
  wireRecall();
  wireGraph();

  if (!bridge) {
    showError("请在 AstrBot 后台的「插件 → memory-core → 视图」里打开这个页面");
    return;
  }
  bridge.onContext(applyTheme);
  bridge.ready().then(applyTheme).catch(() => {}).finally(() => show("overview"));
})();
