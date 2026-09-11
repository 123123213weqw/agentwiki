/* AgentWiki UI -- vanilla JS, no build step, no dependencies.
 *
 * The contract is `wiki.json` produced by agentwiki/export.py.  Everything the
 * UI shows is derived from that one file; the front end never talks to sqlite.
 *
 * Two things are computed here rather than exported, because deriving them
 * client-side is smaller and keeps the export a summary:
 *   - backlinks / related entities, from `session.entity_ids`
 *   - the co-occurrence graph, from the same relation
 */
'use strict';

let WIKI = null;          // the loaded wiki.json
let BY_ID = new Map();    // entity id -> entity
let SES_BY_ID = new Map();// session id -> session
let ENT_IDS = new Set();

const $ = (sel, root = document) => root.querySelector(sel);
const app = () => $('#app');

/* ------------------------------------------------------------------ helpers */

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtNum(n) {
  if (n == null) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function fmtChars(n) {
  if (!n) return '0';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M 字符';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k 字符';
  return n + ' 字符';
}

function fmtDur(ms) {
  if (!ms) return '—';
  if (ms < 1000) return ms + ' ms';
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + ' 秒';
  const m = s / 60;
  if (m < 60) return m.toFixed(1) + ' 分';
  return (m / 60).toFixed(1) + ' 小时';
}

const KIND_LABEL = {
  repo: '仓库', path: '目录', error: '错误', pr: 'PR', issue: 'Issue',
  pkg: '依赖包', domain: '域名', file: '文件', tool: '工具',
};
const kindLabel = k => KIND_LABEL[k] || k;

// Module scope, not inside startGraph(): the legend in viewGraph() needs the
// same mapping, and a second copy would drift.
const KIND_COLOR = {
  repo: '#5eead4', path: '#a78bfa', error: '#f87171', pr: '#fbbf24',
  issue: '#fb923c', pkg: '#60a5fa', domain: '#f472b6', file: '#94a3b8',
};

/* ------------------------------------------------------------------ routing */

function currentRoute() {
  const h = (location.hash || '#/').slice(1);
  const parts = h.split('/').filter(Boolean);
  return { name: parts[0] || 'overview', arg: parts.slice(1).join('/') };
}

function go(hash) { location.hash = hash; }

window.addEventListener('hashchange', render);

/* -------------------------------------------------------------- derivation */

/* session_id -> entity ids present in it.  Built once; used for backlinks,
 * related-entity ranking and the graph. */
let SES2ENT = null;
function sessionEntityMap() {
  if (SES2ENT) return SES2ENT;
  SES2ENT = new Map();
  for (const s of WIKI.sessions) {
    SES2ENT.set(s.id, (s.entity_ids || []).filter(id => BY_ID.has(id)));
  }
  return SES2ENT;
}

/* Entities that co-occur with `entity` across its sessions, ranked by the
 * number of shared sessions.  This is the "related / backlinks" panel. */
function relatedEntities(entity, limit = 24) {
  const s2e = sessionEntityMap();
  const score = new Map();
  for (const sid of entity.session_ids || []) {
    for (const other of s2e.get(sid) || []) {
      if (other === entity.id) continue;
      score.set(other, (score.get(other) || 0) + 1);
    }
  }
  return [...score.entries()]
    .map(([id, n]) => ({ entity: BY_ID.get(id), n }))
    .filter(x => x.entity)
    .sort((a, b) => b.n - a.n || b.entity.mentions - a.entity.mentions)
    .slice(0, limit);
}

/* --------------------------------------------------------------- components */

function statCard(label, value, sub) {
  return `<div class="stat">
    <div class="stat-v">${esc(value)}</div>
    <div class="stat-l">${esc(label)}</div>
    ${sub ? `<div class="stat-s">${esc(sub)}</div>` : ''}
  </div>`;
}

function bar(label, value, max, note) {
  const pct = max > 0 ? Math.max(2, Math.round(value / max * 100)) : 0;
  return `<div class="bar-row">
    <div class="bar-label" title="${esc(label)}">${esc(label)}</div>
    <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
    <div class="bar-val">${esc(note != null ? note : fmtNum(value))}</div>
  </div>`;
}

function kindBadge(kind) {
  return `<span class="kbadge k-${esc(kind)}">${esc(kindLabel(kind))}</span>`;
}

function entityLink(e, extra = '') {
  return `<a class="elink" href="#/entity/${encodeURIComponent(e.id)}">
    ${kindBadge(e.kind)}<span class="elink-name">${esc(e.name)}</span>
    <span class="elink-n">${fmtNum(e.mentions)}</span>${extra}</a>`;
}

/* An inline SVG bar chart.  Used for the timeline. */
function timelineChart(rows) {
  if (!rows.length) return '<p class="muted">没有时间数据。</p>';
  const W = 760, H = 160, pad = 26;
  const max = Math.max(...rows.map(r => r.items));
  const bw = (W - pad * 2) / rows.length;
  const bars = rows.map((r, i) => {
    const h = max ? (r.items / max) * (H - pad * 2) : 0;
    const x = pad + i * bw;
    const y = H - pad - h;
    return `<g class="tl-bar"><rect x="${x + 1}" y="${y}" width="${Math.max(1, bw - 2)}" height="${h}" rx="2">
      <title>${esc(r.month)}: ${fmtNum(r.items)} 条 / ${r.sessions} 会话</title></rect>
      ${rows.length <= 16 || i % Math.ceil(rows.length / 14) === 0
        ? `<text x="${x + bw / 2}" y="${H - pad + 13}" class="tl-x">${esc(r.month.slice(2))}</text>` : ''}
    </g>`;
  }).join('');
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" class="tl-axis"/>
    ${bars}</svg>`;
}

/* ------------------------------------------------------------------- views */

function viewOverview() {
  const s = WIKI.stats, d = WIKI.durations;
  const maxSrc = Math.max(...WIKI.sources.map(x => x.items), 1);
  const topEnt = [...WIKI.entities].sort((a, b) => b.mentions - a.mentions).slice(0, 12);
  const topRepo = WIKI.repos.slice(0, 12);
  const maxRepo = Math.max(...topRepo.map(r => r.sessions), 1);
  const pcts = ['p50', 'p75', 'p90', 'p95'].filter(p => d[p] != null);
  const maxDur = Math.max(...pcts.map(p => d[p]), 1);

  return `
  <section class="hero">
    <h1>知识地形</h1>
    <p class="lede">你的 agent 一直在替你写笔记。这是把它读出来的地方。</p>
    <p class="muted small">L0 采集层 · 全正则抽取 · 零 LLM 调用 · 导出 ${esc(WIKI.meta.generated_at)}</p>
  </section>

  <section class="stats">
    ${statCard('会话', fmtNum(s.sessions), 'sessions')}
    ${statCard('轮次', fmtNum(s.turns), 'turns')}
    ${statCard('条目', fmtNum(s.items), 'items')}
    ${statCard('正文', fmtChars(s.chars), '已脱敏')}
    ${statCard('实体', fmtNum(s.entities), 'regex')}
    ${statCard('提及', fmtNum(s.mentions), 'mentions')}
  </section>

  <div class="grid2">
    <section class="card">
      <h2>来源</h2>
      ${WIKI.sources.map(x => bar(x.source, x.items, maxSrc,
        `${fmtNum(x.items)} 条 · ${x.sessions} 会话`)).join('')}
    </section>

    <section class="card">
      <h2>等待时长分布</h2>
      <p class="muted small">实测 ${fmtNum(d.n)} 个已完成的轮次。p50 = ${esc(fmtDur(d.p50))}。</p>
      ${pcts.map(p => bar(p.toUpperCase(), d[p], maxDur, fmtDur(d[p]))).join('')}
    </section>
  </div>

  <section class="card">
    <h2>时间线</h2>
    ${timelineChart(WIKI.timeline)}
  </section>

  <div class="grid2">
    <section class="card">
      <h2>仓库地形 <span class="muted small">按会话数</span></h2>
      ${topRepo.map(r => bar(r.repo, r.sessions, maxRepo,
        `${r.sessions} 会话`)).join('')}
    </section>

    <section class="card">
      <h2>最热实体 <span class="muted small">按提及数</span></h2>
      <div class="elist">${topEnt.map(e => entityLink(e)).join('')}</div>
    </section>
  </div>`;
}

function viewEntities() {
  const kinds = WIKI.kind_order.filter(k => WIKI.entities.some(e => e.kind === k));
  const counts = Object.fromEntries(kinds.map(k =>
    [k, WIKI.entities.filter(e => e.kind === k).length]));
  return `
  <section class="hero">
    <h1>实体</h1>
    <p class="lede">${fmtNum(WIKI.entities.length)} 个实体。点开任意一个看它的出处与关联。</p>
  </section>
  <div class="toolbar">
    <button class="chip active" data-kind="all">全部 <b>${WIKI.entities.length}</b></button>
    ${kinds.map(k => `<button class="chip" data-kind="${esc(k)}">${esc(kindLabel(k))} <b>${counts[k]}</b></button>`).join('')}
    <select id="ent-sort" class="sel">
      <option value="sessions">按会话数（广度）</option>
      <option value="mentions">按提及数</option>
      <option value="name">按名称</option>
    </select>
  </div>
  <div id="ent-list" class="card ent-table"></div>`;
}

function viewEntity(id) {
  const e = BY_ID.get(id);
  if (!e) return `<section class="card"><h2>未找到</h2><p class="muted">没有实体 <code>${esc(id)}</code>。它可能不在导出的前 ${WIKI.meta.caps.max_entities} 个之内。</p></section>`;

  const sess = (e.session_ids || []).map(sid => SES_BY_ID.get(sid)).filter(Boolean);
  const rel = relatedEntities(e);
  const snips = e.snippets || [];

  return `
  <section class="hero">
    <div class="crumb"><a href="#/entities">实体</a> / ${esc(kindLabel(e.kind))}</div>
    <h1>${kindBadge(e.kind)} ${esc(e.name)}</h1>
    <div class="metaline">
      <span><b>${fmtNum(e.mentions)}</b> 次提及</span>
      <span><b>${e.sessions}</b> 个会话</span>
      ${e.first ? `<span>首次 <b>${esc(e.first)}</b></span>` : ''}
      ${e.last ? `<span>最近 <b>${esc(e.last)}</b></span>` : ''}
    </div>
  </section>

  <div class="grid2">
    <section class="card">
      <h2>出处 <span class="muted small">每条都能追回原始条目</span></h2>
      ${snips.length ? snips.map(s => `
        <blockquote class="snip">
          <div class="snip-meta">
            <a href="#/session/${encodeURIComponent(s.session_id)}">${esc(s.session_id.slice(0, 18))}…</a>
            <span class="src src-${esc(s.source)}">${esc(s.source)}</span>
            ${s.date ? `<span class="muted">${esc(s.date)}</span>` : ''}
            <span class="muted mono">${esc((s.item_id || '').slice(0, 14))}</span>
          </div>
          <div class="snip-text">${esc(s.text)}</div>
        </blockquote>`).join('')
        : '<p class="muted">没有可展示的片段（该实体只在计数中，或未进入片段抓取范围）。</p>'}
    </section>

    <div class="colstack">
      <section class="card">
        <h2>关联实体 <span class="muted small">同会话共现</span></h2>
        ${rel.length ? `<div class="elist">${rel.map(r =>
          entityLink(r.entity, `<span class="shared">↔ ${r.n}</span>`)).join('')}</div>`
          : '<p class="muted">没有共现数据。</p>'}
      </section>

      <section class="card">
        <h2>出现的会话 <span class="muted small">${sess.length}</span></h2>
        <div class="elist">${sess.slice(0, 30).map(s => `
          <a class="elink" href="#/session/${encodeURIComponent(s.id)}">
            <span class="src src-${esc(s.source)}">${esc(s.source)}</span>
            <span class="elink-name">${esc(s.title || s.id)}</span>
            <span class="elink-n">${s.items}</span>
          </a>`).join('')}</div>
      </section>
    </div>
  </div>`;
}

function viewSessions() {
  const rows = WIKI.sessions;
  return `
  <section class="hero">
    <h1>会话</h1>
    <p class="lede">${rows.length} 个会话，按最近活动排序。</p>
  </section>
  <input id="ses-filter" class="bigfilter" type="search" placeholder="过滤标题 / 仓库 / 分支…" autocomplete="off">
  <div id="ses-list" class="card"></div>`;
}

function sessionRows(rows) {
  if (!rows.length) return '<p class="muted">无匹配。</p>';
  return `<table class="tbl"><thead><tr>
      <th>来源</th><th>标题</th><th>仓库</th><th>分支</th><th class="num">条目</th><th class="num">轮次</th><th class="num">tokens</th>
    </tr></thead><tbody>
    ${rows.map(s => `<tr>
      <td><span class="src src-${esc(s.source)}">${esc(s.source)}</span></td>
      <td><a href="#/session/${encodeURIComponent(s.id)}">${esc(s.title || s.id)}</a></td>
      <td class="mono">${esc(s.repo || '—')}</td>
      <td class="mono">${esc(s.branch || '—')}</td>
      <td class="num">${fmtNum(s.items)}</td>
      <td class="num">${fmtNum(s.turns)}</td>
      <td class="num">${s.tokens ? fmtNum(s.tokens) : '—'}</td>
    </tr>`).join('')}
    </tbody></table>`;
}

function viewSession(id) {
  const s = SES_BY_ID.get(id);
  if (!s) return `<section class="card"><h2>未找到</h2><p class="muted">没有会话 <code>${esc(id)}</code>。</p></section>`;
  const ents = (s.entity_ids || []).map(eid => BY_ID.get(eid)).filter(Boolean)
    .sort((a, b) => b.mentions - a.mentions);
  return `
  <section class="hero">
    <div class="crumb"><a href="#/sessions">会话</a> / <span class="src src-${esc(s.source)}">${esc(s.source)}</span></div>
    <h1>${esc(s.title || s.id)}</h1>
    <div class="metaline">
      <span><b>${fmtNum(s.items)}</b> 条目</span>
      <span><b>${fmtNum(s.turns)}</b> 轮次</span>
      ${s.tokens ? `<span><b>${fmtNum(s.tokens)}</b> tokens</span>` : ''}
      ${s.created ? `<span>${esc(s.created)}</span>` : ''}
    </div>
    ${s.cwd ? `<div class="mono small muted cwd">${esc(s.cwd)}</div>` : ''}
  </section>
  <section class="card">
    <h2>这个会话涉及 ${ents.length} 个实体</h2>
    ${ents.length ? `<div class="elist">${ents.map(e => entityLink(e)).join('')}</div>`
      : '<p class="muted">没有实体记录。</p>'}
    <p class="muted small">原始文本未导出（体积原因）。用 <code>agentwiki shell</code> 查这个会话的条目：
      <code>SELECT item_type, substr(text,1,200) FROM items WHERE session_id='${esc(s.id)}'</code></p>
  </section>`;
}

/* --------------------------------------------------------------- the graph */

function viewGraph() {
  const legend = WIKI.kind_order
    .map(k => `<span class="lg-item"><i class="lg-dot" style="background:${KIND_COLOR[k] || '#94a3b8'}"></i>${esc(kindLabel(k))}</span>`)
    .join('');
  return `
  <section class="hero">
    <h1>图谱</h1>
    <p class="lede">一个点 = 一个实体；两个实体在<strong>同一个会话</strong>里出现过就连一条边。
       所以挨得近的一团，就是"你总是连着一起碰的东西"。</p>
  </section>
  <div class="toolbar">
    <label class="muted small">节点数
      <select id="g-n" class="sel">
        <option value="80">80</option><option value="150" selected>150</option>
        <option value="300">300</option><option value="500">500</option>
      </select></label>
    <label class="muted small"><input type="checkbox" id="g-labels" checked> 显示标签</label>
    <span class="muted small" id="g-info"></span>
  </div>
  <div class="card graphwrap"><canvas id="graph"></canvas></div>
  <div class="legend">
    <div class="lg-row"><span class="lg-key">颜色 = 类型</span>${legend}</div>
    <div class="lg-row"><span class="lg-key">大小 = 提及次数</span>
      <span class="lg-item"><i class="lg-dot lg-sm"></i>少</span>
      <span class="lg-item"><i class="lg-dot lg-lg"></i>多</span>
    </div>
    <div class="lg-row"><span class="lg-key">怎么用</span>
      <span class="lg-item">拖拽移动</span>
      <span class="lg-item">悬停看名字</span>
      <span class="lg-item">点一下进入它的页面</span>
    </div>
  </div>`;
}

let graphStop = null;

function startGraph() {
  const canvas = $('#graph');
  if (!canvas) return;
  if (graphStop) graphStop();
  const ctx = canvas.getContext('2d');
  const nSel = $('#g-n'), labels = $('#g-labels'), info = $('#g-info');

  // Labelling every node turns the dense centre into overlapping text, so only
  // the largest hubs keep a permanent label.  Anything hovered is labelled on
  // demand.
  const LABEL_BUDGET = 20;
  let nodes = [], links = [], raf = null, dragNode = null, hoverNode = null;

  function build() {
    const N = +nSel.value;
    const top = [...WIKI.entities].sort((a, b) => b.mentions - a.mentions).slice(0, N);
    const keep = new Set(top.map(e => e.id));
    nodes = top.map((e, i) => {
      const a = Math.random() * Math.PI * 2, r = 60 + Math.random() * 240;
      return { e, x: W / 2 + Math.cos(a) * r, y: H / 2 + Math.sin(a) * r,
               vx: 0, vy: 0, d: 0, label: i < LABEL_BUDGET };
    });
    const idx = new Map(nodes.map((n, i) => [n.e.id, i]));
    const s2e = sessionEntityMap();
    const w = new Map();
    for (const s of WIKI.sessions) {
      const ids = (s2e.get(s.id) || []).filter(id => keep.has(id));
      for (let i = 0; i < ids.length; i++)
        for (let j = i + 1; j < ids.length; j++) {
          const a = idx.get(ids[i]), b = idx.get(ids[j]);
          if (a == null || b == null) continue;
          const k = a < b ? a + ':' + b : b + ':' + a;
          w.set(k, (w.get(k) || 0) + 1);
        }
    }
    links = [...w.entries()].map(([k, v]) => {
      const [a, b] = k.split(':').map(Number);
      return { a, b, v };
    }).filter(l => l.v >= 2);
    if (info) info.textContent = `${nodes.length} 个节点 · ${links.length} 条边（共现 ≥2 次）`;
  }

  let W = 900, H = 560;
  function resize() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(320, r.width); H = Math.max(320, r.height);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function step() {
    // A deliberately simple Fruchterman-Reingold style pass: repulsion between
    // all pairs, springs along edges, weak centring.  O(n^2) is fine at n<=500.
    for (const n of nodes) { n.vx *= 0.82; n.vy *= 0.82; }
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
        const f = 900 / d2;
        const d = Math.sqrt(d2);
        const fx = dx / d * f, fy = dy / d * f;
        a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
      }
    }
    for (const l of links) {
      const a = nodes[l.a], b = nodes[l.b];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.max(1, Math.hypot(dx, dy));
      const f = (d - 70) * 0.012 * Math.min(3, l.v);
      const fx = dx / d * f, fy = dy / d * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * 0.0016;
      n.vy += (H / 2 - n.y) * 0.0016;
      n.x += Math.max(-12, Math.min(12, n.vx));
      n.y += Math.max(-12, Math.min(12, n.vy));
      n.x = Math.max(14, Math.min(W - 14, n.x));
      n.y = Math.max(14, Math.min(H - 14, n.y));
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.lineWidth = 1;
    for (const l of links) {
      const a = nodes[l.a], b = nodes[l.b];
      ctx.strokeStyle = `rgba(148,163,184,${Math.min(0.45, 0.06 + l.v * 0.03)})`;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    const showLabels = labels && labels.checked;
    for (const n of nodes) {
      const r = 3 + Math.min(13, Math.sqrt(n.e.mentions) * 0.85);
      ctx.fillStyle = KIND_COLOR[n.e.kind] || '#94a3b8';
      ctx.globalAlpha = 0.9;
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      if (showLabels && (n.label || n === hoverNode || n === dragNode)) {
        ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
        const text = n.e.name.slice(0, 26);
        const tx = n.x + r + 3, ty = n.y + 3.5;
        // A dark halo keeps the label legible where it crosses edges.
        ctx.lineWidth = 3;
        ctx.strokeStyle = 'rgba(9,12,18,.85)';
        ctx.strokeText(text, tx, ty);
        ctx.fillStyle = n === hoverNode ? '#5eead4' : 'rgba(226,232,240,.86)';
        ctx.fillText(text, tx, ty);
      }
      n._r = r;
    }
    if (dragNode) {
      ctx.strokeStyle = '#5eead4'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(dragNode.x, dragNode.y, dragNode._r + 4, 0, Math.PI * 2); ctx.stroke();
    }
  }

  function loop() { step(); draw(); raf = requestAnimationFrame(loop); }

  function hit(ev) {
    const r = canvas.getBoundingClientRect();
    const mx = ev.clientX - r.left, my = ev.clientY - r.top;
    let best = null, bd = 1e9;
    for (const n of nodes) {
      const d = Math.hypot(n.x - mx, n.y - my);
      if (d < (n._r || 6) + 8 && d < bd) { bd = d; best = n; }
    }
    return best;
  }

  const onDown = ev => { dragNode = hit(ev); if (dragNode) canvas.style.cursor = 'grabbing'; };
  const onMove = ev => {
    if (dragNode) {
      const r = canvas.getBoundingClientRect();
      dragNode.x = ev.clientX - r.left; dragNode.y = ev.clientY - r.top;
      dragNode.vx = dragNode.vy = 0;
    } else {
      const h = hit(ev);
      hoverNode = h;
      canvas.style.cursor = h ? 'pointer' : 'default';
    }
  };
  const onUp = ev => {
    if (dragNode) {
      const n = dragNode; dragNode = null; canvas.style.cursor = 'default';
      // A click (rather than a drag) opens the entity.
      if (Math.hypot(n.x - (ev.clientX - canvas.getBoundingClientRect().left),
                     n.y - (ev.clientY - canvas.getBoundingClientRect().top)) < 24) {
        go('#/entity/' + encodeURIComponent(n.e.id));
      }
      return;
    }
    const n = hit(ev);
    if (n) go('#/entity/' + encodeURIComponent(n.e.id));
  };

  resize(); build(); loop();
  window.addEventListener('resize', resize);
  canvas.addEventListener('mousedown', onDown);
  canvas.addEventListener('mousemove', onMove);
  canvas.addEventListener('mouseup', onUp);
  if (nSel) nSel.addEventListener('change', build);
  if (labels) labels.addEventListener('change', () => {});

  graphStop = () => {
    if (raf) cancelAnimationFrame(raf);
    window.removeEventListener('resize', resize);
    canvas.removeEventListener('mousedown', onDown);
    canvas.removeEventListener('mousemove', onMove);
    canvas.removeEventListener('mouseup', onUp);
    if (nSel) nSel.removeEventListener('change', build);
  };
}

/* --------------------------------------------------------- list interaction */

function wireEntities() {
  const listEl = $('#ent-list');
  const sortSel = $('#ent-sort');
  let kind = 'all';

  function draw() {
    let rows = WIKI.entities;
    if (kind !== 'all') rows = rows.filter(e => e.kind === kind);
    const key = sortSel.value;
    rows = [...rows].sort((a, b) =>
      key === 'name' ? a.name.localeCompare(b.name)
        : key === 'sessions' ? b.sessions - a.sessions
          : b.mentions - a.mentions);
    rows = rows.slice(0, 300);
    listEl.innerHTML = `<table class="tbl"><thead><tr>
        <th>类型</th><th>实体</th><th class="num">提及</th><th class="num">会话</th><th>跨度</th>
      </tr></thead><tbody>
      ${rows.map(e => `<tr>
        <td>${kindBadge(e.kind)}</td>
        <td><a href="#/entity/${encodeURIComponent(e.id)}">${esc(e.name)}</a></td>
        <td class="num">${fmtNum(e.mentions)}</td>
        <td class="num">${e.sessions}</td>
        <td class="muted small">${esc(e.first || '—')} → ${esc(e.last || '—')}</td>
      </tr>`).join('')}</tbody></table>
      <p class="muted small">显示前 ${rows.length} 个（共 ${WIKI.entities.length}，导出上限 ${WIKI.meta.caps.max_entities}）。</p>`;
  }

  document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    kind = c.dataset.kind;
    draw();
  }));
  sortSel.addEventListener('change', draw);
  draw();
}

function wireSessions() {
  const inp = $('#ses-filter'), listEl = $('#ses-list');
  if (!inp) return;
  const draw = () => {
    const q = inp.value.trim().toLowerCase();
    const rows = !q ? WIKI.sessions : WIKI.sessions.filter(s =>
      [s.title, s.repo, s.branch, s.cwd, s.id, s.source]
        .some(v => (v || '').toLowerCase().includes(q)));
    listEl.innerHTML = sessionRows(rows);
  };
  inp.addEventListener('input', draw);
  draw();
}

/* -------------------------------------------------------------- search box */

function wireSearch() {
  const inp = $('#search'), box = $('#search-results');
  let items = [], sel = -1;

  function results(q) {
    q = q.trim().toLowerCase();
    if (!q) return [];
    const ents = WIKI.entities
      .filter(e => e.name.toLowerCase().includes(q))
      .sort((a, b) => {
        const ai = a.name.toLowerCase().indexOf(q), bi = b.name.toLowerCase().indexOf(q);
        return ai - bi || b.mentions - a.mentions;
      }).slice(0, 12)
      .map(e => ({
        label: e.name, kind: kindLabel(e.kind), n: e.mentions,
        href: '#/entity/' + encodeURIComponent(e.id),
      }));
    const ses = WIKI.sessions
      .filter(s => (s.title || '').toLowerCase().includes(q) || s.id.includes(q))
      .slice(0, 6)
      .map(s => ({
        label: s.title || s.id, kind: s.source, n: s.items,
        href: '#/session/' + encodeURIComponent(s.id),
      }));
    return [...ents, ...ses];
  }

  function close() { box.hidden = true; sel = -1; }

  function draw() {
    if (!items.length) return close();
    box.innerHTML = items.map((it, i) => `
      <a class="sres${i === sel ? ' active' : ''}" href="${it.href}">
        <span class="sres-kind">${esc(it.kind)}</span>
        <span class="sres-label">${esc(it.label)}</span>
        <span class="sres-n">${fmtNum(it.n)}</span>
      </a>`).join('');
    box.hidden = false;
  }

  inp.addEventListener('input', () => { items = results(inp.value); sel = -1; draw(); });
  inp.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { inp.value = ''; close(); inp.blur(); return; }
    if (!items.length) return;
    if (ev.key === 'ArrowDown') { sel = (sel + 1) % items.length; draw(); ev.preventDefault(); }
    else if (ev.key === 'ArrowUp') { sel = (sel - 1 + items.length) % items.length; draw(); ev.preventDefault(); }
    else if (ev.key === 'Enter') { go(items[sel < 0 ? 0 : sel].href); inp.value = ''; close(); }
  });
  box.addEventListener('click', () => { inp.value = ''; close(); });
  document.addEventListener('click', ev => {
    if (!$('.search-wrap').contains(ev.target)) close();
  });
  document.addEventListener('keydown', ev => {
    if (ev.key === '/' && document.activeElement !== inp) { inp.focus(); ev.preventDefault(); }
  });
}

/* ------------------------------------------------------------------- render */

function render() {
  if (!WIKI) return;
  if (graphStop) { graphStop(); graphStop = null; }

  const { name, arg } = currentRoute();
  let html;
  switch (name) {
    case 'entities': html = viewEntities(); break;
    case 'entity': html = viewEntity(decodeURIComponent(arg)); break;
    case 'sessions': html = viewSessions(); break;
    case 'session': html = viewSession(decodeURIComponent(arg)); break;
    case 'graph': html = viewGraph(); break;
    default: html = viewOverview();
  }
  app().innerHTML = html;

  document.querySelectorAll('.nav a').forEach(a =>
    a.classList.toggle('active', a.dataset.route === '/' + (name === 'overview' ? '' : name)));

  if (name === 'entities') wireEntities();
  if (name === 'sessions') wireSessions();
  if (name === 'graph') startGraph();

  window.scrollTo(0, 0);
}

/* --------------------------------------------------------------------- boot */

async function boot() {
  try {
    const res = await fetch('wiki.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    WIKI = await res.json();
  } catch (err) {
    app().innerHTML = `<section class="card error"><h2>无法加载 wiki.json</h2>
      <p class="muted">${esc(err.message)}</p>
      <p class="muted">先运行：<code>python -m agentwiki export</code></p></section>`;
    return;
  }

  BY_ID = new Map(WIKI.entities.map(e => [e.id, e]));
  ENT_IDS = new Set(BY_ID.keys());
  SES_BY_ID = new Map(WIKI.sessions.map(s => [s.id, s]));

  $('#foot-meta').textContent =
    `${WIKI.meta.generated_at} · 导出 v${WIKI.meta.export_version} · `
    + `${WIKI.entities.length} 实体 / ${WIKI.sessions.length} 会话`;

  wireSearch();
  render();
}

boot();
