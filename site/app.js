/* SAFS Connector explorer. Every view is computed in the browser from data/works.json,
   so the year and theme filters apply everywhere. */
"use strict";

const S = {
  meta: null, works: [], filtered: [],
  fac: new Map(), theme: new Map(), sub: new Map(), org: {},
  tab: "network", facSlug: null, netSel: null,
};
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = d3.format(",");

/* ------------------------------------------------------------------ load */
Promise.all([d3.json("data/meta.json"), d3.json("data/works.json")]).then(([meta, w]) => {
  S.meta = meta;
  S.works = w.rows.map((r) => Object.fromEntries(w.cols.map((c, i) => [c, r[i]])));
  meta.faculty.forEach((f) => S.fac.set(f.slug, f));
  meta.themes.forEach((t) => S.theme.set(t.id, t));
  meta.subtopics.forEach((t) => S.sub.set(t.id, t));
  S.org = meta.orgs;
  S.works.forEach((x) => {
    x.themesAny = new Set([x.theme, ...x.subtopics.map((s) => S.sub.get(s)?.theme)].filter(Boolean));
    x.text = [x.title, x.summary, x.venue, ...x.species, ...x.regions, ...x.taxa, ...x.ecosystems,
      ...x.subtopics.map((s) => S.sub.get(s)?.name)].join(" ").toLowerCase();
  });
  $("#built").textContent = meta.built;
  initFilters();
  initTabs();
  initFaculty();
  initSearch();
  initNetwork();
  initMaps(); // maps.js
  readHash();
  apply();
}).catch((e) => {
  $("main").innerHTML = `<p>Could not load the data (${esc(e.message)}). If you opened index.html from disk, serve the folder instead, e.g. <code>python -m http.server</code> in site/.</p>`;
});

/* ------------------------------------------------------------------ filters */
function initFilters() {
  const years = d3.extent(S.works, (d) => d.year);
  const opts = d3.range(years[0], years[1] + 1).map((y) => `<option>${y}</option>`).join("");
  $("#f-from").innerHTML = opts; $("#f-to").innerHTML = opts;
  $("#f-from").value = years[0]; $("#f-to").value = years[1];
  $("#f-theme").insertAdjacentHTML("beforeend",
    S.meta.themes.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join(""));
  ["#f-from", "#f-to", "#f-theme"].forEach((s) => $(s).addEventListener("change", apply));
}

function apply() {
  let a = +$("#f-from").value, b = +$("#f-to").value;
  if (a > b) [a, b] = [b, a];
  const th = $("#f-theme").value;
  S.filtered = S.works.filter((w) => w.year >= a && w.year <= b && (!th || w.themesAny.has(th)));
  $("#f-count").textContent = `${fmt(S.filtered.length)} of ${fmt(S.works.length)} papers`;
  renderAll();
}
function renderAll() {
  if (S.tab === "network") renderNetwork();
  if (S.tab === "faculty") renderFaculty();
  if (S.tab === "search") renderSearch();
  if (S.tab === "map") renderMap();        // maps.js
  if (S.tab === "themes") renderThemeMap(); // maps.js
}

/* ------------------------------------------------------------------ tabs & hash */
function initTabs() {
  document.querySelectorAll(".tabs button").forEach((b) =>
    b.addEventListener("click", () => setTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => { readHash(); renderAll(); });
}
function setTab(tab, push = true) {
  S.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== `tab-${tab}`; });
  if (push) history.replaceState(null, "", tab === "faculty" && S.facSlug ? `#faculty/${S.facSlug}` : `#${tab}`);
  renderAll();
}
function readHash() {
  const [tab, slug] = location.hash.slice(1).split("/");
  if (tab === "faculty" && slug && S.fac.has(slug)) S.facSlug = slug;
  if (["network", "map", "themes", "faculty", "search"].includes(tab)) setTab(tab, false);
}
function openFaculty(slug) {
  S.facSlug = slug;
  history.replaceState(null, "", `#faculty/${slug}`);
  setTab("faculty", false);
  window.scrollTo({ top: 0 });
}

/* ------------------------------------------------------------------ tooltip */
const tip = $("#tip");
function showTip(ev, html) {
  tip.innerHTML = html; tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 14;
  tip.style.left = `${x}px`; tip.style.top = `${y}px`;
}
const hideTip = () => { tip.hidden = true; };

/* ------------------------------------------------------------------ shared helpers */
const countBy = (arr, keyFn) => {
  const m = new Map();
  arr.forEach((x) => [].concat(keyFn(x) ?? []).forEach((k) => m.set(k, (m.get(k) || 0) + 1)));
  return m;
};
const topN = (m, n) => [...m.entries()].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(b[0])).slice(0, n);
const orgName = (id) => S.org[id]?.name ?? id;
const doiLink = (w) => (w.doi ? `https://doi.org/${w.doi}` : `https://openalex.org/${w.id}`);

function paperList(works, limit = 60) {
  const sorted = [...works].sort((a, b) => b.year - a.year || b.cited_by - a.cited_by);
  const items = sorted.slice(0, limit).map((w) => `<li><a href="${doiLink(w)}" target="_blank" rel="noopener">${esc(w.title)}</a>
    <div class="meta">${w.year}${w.venue ? " · " + esc(w.venue) : ""} · ${w.faculty.map((s) => esc(S.fac.get(s)?.name)).join(", ")}</div></li>`).join("");
  const more = works.length > limit ? `<p class="muted">Showing the ${limit} most recent of ${fmt(works.length)}. Use Search for the full list.</p>` : "";
  return `<ul class="papers">${items}</ul>${more}`;
}

function barList(entries, { label, onClick } = {}) {
  if (!entries.length) return `<p class="muted">None in this filter.</p>`;
  const max = d3.max(entries, (d) => d[1]);
  return `<div class="bars">${entries.map(([k, n]) => `<div class="row" title="${esc(label(k))}: ${n} papers">
    <span class="lab">${onClick ? `<button data-k="${esc(k)}">${esc(label(k))}</button>` : esc(label(k))}</span>
    <span class="track"><span class="bar" style="display:block;width:${(100 * n) / max}%"></span></span>
    <span class="n">${n}</span></div>`).join("")}</div>`;
}

/* ------------------------------------------------------------------ NETWORK */
let net = {};
function initNetwork() {
  const svg = d3.select("#net");
  net.g = svg.append("g");
  net.gEdges = net.g.append("g");
  net.gNodes = net.g.append("g");
  svg.call(d3.zoom().scaleExtent([0.2, 5]).on("zoom", (e) => net.g.attr("transform", e.transform)));
  svg.on("click", (e) => { if (e.target === svg.node()) selectNode(null); });
  net.sim = d3.forceSimulation()
    .force("charge", d3.forceManyBody().strength(-220).distanceMax(400))
    .force("collide", d3.forceCollide().radius((d) => d.r + (d.type === "theme" ? 12 : 4)))
    .on("tick", drawPositions);
  for (const [id, out] of [["#n-orgs", "#n-orgs-v"], ["#n-min", "#n-min-v"]]) {
    $(id).addEventListener("input", () => { $(out).textContent = $(id).value; renderNetwork(); });
  }
  $("#n-ff").addEventListener("change", renderNetwork);
  net.pos = new Map();
}

function drawPositions() {
  net.edgeSel?.attr("x1", (d) => d.source.x).attr("y1", (d) => d.source.y)
    .attr("x2", (d) => d.target.x).attr("y2", (d) => d.target.y);
  net.nodeSel?.attr("transform", (d) => `translate(${d.x},${d.y})`);
}

function buildGraph() {
  const minW = +$("#n-min").value, nOrgs = +$("#n-orgs").value, ff = $("#n-ff").checked;
  const W = S.filtered;
  const facCount = countBy(W, (w) => w.faculty);
  const themeCount = countBy(W, (w) => w.theme);
  const orgCount = countBy(W, (w) => w.orgs);
  const keepOrgs = new Set(topN(orgCount, nOrgs).map((d) => d[0]));
  const edges = new Map();
  const add = (a, b) => { const k = a < b ? `${a}|${b}` : `${b}|${a}`; edges.set(k, (edges.get(k) || 0) + 1); };
  for (const w of W) {
    for (const f of w.faculty) {
      if (w.theme) add(`f:${f}`, `t:${w.theme}`);
      for (const o of w.orgs) if (keepOrgs.has(o)) add(`f:${f}`, `o:${o}`);
    }
    if (ff && w.faculty.length > 1)
      for (let i = 0; i < w.faculty.length; i++)
        for (let j = i + 1; j < w.faculty.length; j++) add(`f:${w.faculty[i]}`, `f:${w.faculty[j]}`);
  }
  const links = [...edges].filter(([, n]) => n >= minW).map(([k, n]) => { const [s, t] = k.split("|"); return { source: s, target: t, n }; });
  const used = new Set(links.flatMap((l) => [l.source, l.target]));
  const r = d3.scaleSqrt().domain([1, d3.max([...facCount.values(), ...themeCount.values(), 1])]).range([4, 26]);
  const nodes = [];
  for (const [f, n] of facCount) if (used.has(`f:${f}`)) nodes.push({ id: `f:${f}`, key: f, type: "faculty", label: S.fac.get(f).name, n });
  for (const [t, n] of themeCount) if (used.has(`t:${t}`)) nodes.push({ id: `t:${t}`, key: t, type: "theme", label: S.theme.get(t).name, n });
  for (const o of keepOrgs) if (used.has(`o:${o}`)) nodes.push({ id: `o:${o}`, key: o, type: "org", label: orgName(o), n: orgCount.get(o) });
  nodes.forEach((d) => { d.r = r(d.n); const p = net.pos.get(d.id); if (p) { d.x = p.x; d.y = p.y; } });
  return { nodes, links };
}

function renderNetwork() {
  const svg = d3.select("#net");
  const { width, height } = svg.node().getBoundingClientRect();
  if (!width) return;
  const { nodes, links } = buildGraph();
  net.nodes = nodes; net.links = links;
  const xFor = { theme: width * 0.22, faculty: width * 0.5, org: width * 0.76 };
  const w = d3.scaleLinear().domain([1, d3.max(links, (l) => l.n) || 1]).range([0.6, 6]);

  net.edgeSel = net.gEdges.selectAll("line").data(links, (d) => `${d.source.id ?? d.source}|${d.target.id ?? d.target}`)
    .join("line").attr("class", "edge").attr("stroke-width", (d) => w(d.n));
  net.nodeSel = net.gNodes.selectAll("g.node").data(nodes, (d) => d.id)
    .join((enter) => {
      const g = enter.append("g");
      g.append("circle");
      g.append("text").attr("dy", "0.32em");
      return g;
    })
    .attr("class", (d) => `node ${d.type}`)
    .on("click", (e, d) => { e.stopPropagation(); selectNode(d.id); })
    .on("mousemove", (e, d) => showTip(e, `<b>${esc(d.label)}</b>${fmt(d.n)} papers${d.type === "org" && S.org[d.key]?.country ? " · " + esc(S.org[d.key].country) : ""}`))
    .on("mouseleave", hideTip)
    .call(d3.drag()
      .on("start", (e, d) => { if (!e.active) net.sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) net.sim.alphaTarget(0); d.fx = null; d.fy = null; }));
  net.nodeSel.select("circle").attr("r", (d) => d.r);
  net.nodeSel.select("text")
    .attr("x", (d) => (d.type === "theme" ? -(d.r + 4) : d.r + 4))
    .attr("text-anchor", (d) => (d.type === "theme" ? "end" : "start"))
    .text((d) => (d.type === "org" && d.r < 9 ? "" : shortLabel(d)));

  net.sim.nodes(nodes)
    .force("link", d3.forceLink(links).id((d) => d.id).distance(90).strength((l) => Math.min(0.3, 0.02 + l.n / 150)))
    .force("x", d3.forceX((d) => xFor[d.type]).strength(0.45))
    .force("y", d3.forceY(height / 2).strength(0.04))
    .stop();
  // Settle the layout up front (animation frames are throttled in background tabs),
  // starting nodes in their band so the three columns form quickly.
  nodes.forEach((d) => { if (d.x == null) { d.x = xFor[d.type] + (Math.random() - 0.5) * 60; d.y = height / 2 + (Math.random() - 0.5) * height * 0.6; } });
  net.sim.alpha(net.pos.size ? 0.4 : 1);
  for (let i = 0, n = net.pos.size ? 120 : 300; i < n; i++) net.sim.tick();
  drawPositions();
  nodes.forEach((d) => net.pos.set(d.id, { x: d.x, y: d.y }));
  if (S.netSel && !nodes.find((n) => n.id === S.netSel)) S.netSel = null;
  selectNode(S.netSel);
}
function shortLabel(d) {
  if (d.type === "faculty") return d.label.split(" ").slice(-1)[0];
  return d.label.length > 34 ? d.label.slice(0, 32) + "…" : d.label;
}

function worksFor(id) {
  const [kind, key] = [id.slice(0, 1), id.slice(2)];
  return S.filtered.filter((w) => (kind === "f" ? w.faculty.includes(key) : kind === "t" ? w.theme === key : w.orgs.includes(key)));
}
function worksForPair(a, b) {
  const A = new Set(worksFor(a).map((w) => w.id));
  return worksFor(b).filter((w) => A.has(w.id));
}

function selectNode(id) {
  S.netSel = id;
  const panel = $("#net-panel");
  if (!id) {
    net.nodeSel?.classed("dim", false).classed("sel", false);
    net.edgeSel?.classed("dim", false).classed("hl", false);
    panel.innerHTML = `<p class="muted">Click a circle to see its connections and the papers behind them. Drag to pan, scroll to zoom.</p>
      <p class="muted">Circles sit in three bands: research themes on the left, SAFS faculty in the middle, and partner organizations on the right. Line width shows the number of shared papers.</p>`;
    return;
  }
  const nb = new Set([id]);
  net.links.forEach((l) => { if (l.source.id === id) nb.add(l.target.id); if (l.target.id === id) nb.add(l.source.id); });
  net.nodeSel.classed("dim", (d) => !nb.has(d.id)).classed("sel", (d) => d.id === id);
  net.edgeSel.classed("dim", (l) => l.source.id !== id && l.target.id !== id)
    .classed("hl", (l) => l.source.id === id || l.target.id === id);

  const node = net.nodes.find((n) => n.id === id);
  const conns = net.links.filter((l) => l.source.id === id || l.target.id === id)
    .map((l) => ({ other: l.source.id === id ? l.target : l.source, n: l.n }))
    .sort((a, b) => b.n - a.n);
  const kindName = { faculty: "SAFS faculty", theme: "Research theme", org: "Partner organization" }[node.type];
  const extra = node.type === "faculty" ? ` · <a href="#faculty/${node.key}" data-fac="${node.key}">Faculty card</a>`
    : node.type === "org" && S.org[node.key] ? ` · ${esc([S.org[node.key].city, S.org[node.key].country].filter(Boolean).join(", "))}` : "";
  const groups = d3.groups(conns, (c) => c.other.type);
  panel.innerHTML = `<div class="kind">${kindName}</div><h2>${esc(node.label)}</h2>
    <p class="muted">${fmt(node.n)} papers in this filter${extra}</p>
    ${groups.map(([t, cs]) => `<h3>${{ faculty: "Faculty", theme: "Themes", org: "Partners" }[t]}</h3>
      <ul class="conn">${cs.map((c) => `<li><button data-pair="${c.other.id}">${esc(c.other.label)}</button><span class="n">${c.n}</span></li>`).join("")}</ul>`).join("")}
    <div id="pair-papers"><h3>Papers</h3>${paperList(worksFor(id), 40)}</div>`;
  panel.querySelectorAll("[data-pair]").forEach((b) => b.addEventListener("click", () => {
    const other = net.nodes.find((n) => n.id === b.dataset.pair);
    const ws = worksForPair(id, other.id);
    $("#pair-papers").innerHTML = `<h3>Papers: ${esc(node.label)} + ${esc(other.label)}</h3>
      <p><button class="btn" id="pair-go">Focus on ${esc(other.label)}</button></p>${paperList(ws, 60)}`;
    $("#pair-go").addEventListener("click", () => selectNode(other.id));
    $("#pair-papers").scrollIntoView({ block: "nearest" });
  }));
  panel.querySelector("[data-fac]")?.addEventListener("click", (e) => { e.preventDefault(); openFaculty(node.key); });
}

/* ------------------------------------------------------------------ FACULTY */
function initFaculty() {
  const fs = S.meta.faculty;
  $("#fac-select").innerHTML = `<option value="">Choose…</option>` +
    fs.map((f) => `<option value="${f.slug}">${esc(f.name)}</option>`).join("");
  $("#fac-select").addEventListener("change", (e) => { if (e.target.value) openFaculty(e.target.value); else { S.facSlug = null; history.replaceState(null, "", "#faculty"); renderFaculty(); } });
}

function renderFaculty() {
  $("#fac-select").value = S.facSlug || "";
  const counts = countBy(S.filtered, (w) => w.faculty);
  const grid = $("#fac-grid"), card = $("#fac-card");
  if (!S.facSlug) {
    card.hidden = true; grid.hidden = false;
    grid.innerHTML = S.meta.faculty.map((f) => `<button data-slug="${f.slug}">${esc(f.name)}
      <span class="n">${esc(rankName(f.rank))} · ${fmt(counts.get(f.slug) || 0)} papers</span></button>`).join("");
    grid.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => openFaculty(b.dataset.slug)));
    return;
  }
  grid.hidden = true; card.hidden = false;
  const f = S.fac.get(S.facSlug);
  const W = S.filtered.filter((w) => w.faculty.includes(f.slug));
  const years = d3.extent(W, (w) => w.year);
  const orgs = countBy(W, (w) => w.orgs);
  const co = countBy(W, (w) => w.faculty.filter((s) => s !== f.slug));
  const themes = countBy(W, (w) => w.theme);
  const subs = countBy(W, (w) => w.subtopics);
  const taxa = countBy(W, (w) => w.taxa.filter((t) => t !== "none"));
  const species = countBy(W, (w) => w.species);
  const countries = new Set([...orgs.keys()].map((o) => S.org[o]?.country).filter(Boolean));

  card.innerHTML = `<div class="head"><div><h2>${esc(f.name)}</h2>
      <div class="muted">${esc(rankName(f.rank))}${f.areas.length ? " · " + f.areas.map(esc).join(", ") : ""}</div></div>
      <div><a href="${f.url}" target="_blank" rel="noopener">SAFS profile</a>${f.scholar ? ` · <a href="https://scholar.google.com/citations?user=${f.scholar}" target="_blank" rel="noopener">Google Scholar</a>` : ""}
      · <a href="#faculty" id="fac-back">All faculty</a></div></div>
    <div class="tiles">
      <div class="tile"><div class="v">${fmt(W.length)}</div><div class="l">${W.length === 1 ? "paper" : "papers"}${years[0] ? ` (${years[0]}–${years[1]})` : ""}</div></div>
      <div class="tile"><div class="v">${fmt(orgs.size)}</div><div class="l">partner organizations</div></div>
      <div class="tile"><div class="v">${fmt(countries.size)}</div><div class="l">partner countries</div></div>
      <div class="tile"><div class="v">${fmt(co.size)}</div><div class="l">SAFS co-authors</div></div>
    </div>
    <div class="card-grid">
      <section class="wide"><h3>Papers per year</h3><svg id="timeline" width="100%" height="150" role="img" aria-label="Papers per year"></svg></section>
      <section><h3>Research themes (primary)</h3><div id="c-themes">${barList(topN(themes, 15), { label: (k) => S.theme.get(k)?.name ?? k })}</div></section>
      <section><h3>Top subtopics</h3>${barList(topN(subs, 10), { label: (k) => S.sub.get(k)?.name ?? k })}</section>
      <section><h3>Top partner organizations</h3>${barList(topN(orgs, 12), { label: orgName })}</section>
      <section><h3>SAFS co-authors</h3><div id="c-co">${barList(topN(co, 12), { label: (k) => S.fac.get(k)?.name ?? k, onClick: true })}</div></section>
      <section><h3>Organisms</h3><div class="chips">${topN(taxa, 10).map(([k, n]) => `<span class="chip">${esc(k.replace(/-/g, " "))} · ${n}</span>`).join("") || '<span class="muted">None</span>'}</div>
        <h3 style="margin-top:14px">Most-studied species</h3><div class="chips">${topN(species, 10).map(([k, n]) => `<span class="chip">${esc(k)} · ${n}</span>`).join("") || '<span class="muted">None</span>'}</div></section>
      <section><h3>Most-cited papers</h3>${paperList([...W].sort((a, b) => b.cited_by - a.cited_by).slice(0, 8).sort((a, b) => b.year - a.year), 8)}</section>
    </div>`;
  card.querySelectorAll("#c-co button").forEach((b) => b.addEventListener("click", () => openFaculty(b.dataset.k)));
  $("#fac-back").addEventListener("click", (e) => { e.preventDefault(); S.facSlug = null; history.replaceState(null, "", "#faculty"); renderFaculty(); });
  drawTimeline(W);
}
const rankName = (r) => ({ professor: "Professor", associate: "Associate Professor", assistant: "Assistant Professor", teaching: "Teaching faculty", other: "Research / affiliate faculty" }[r] || r || "");

function drawTimeline(W) {
  const svg = d3.select("#timeline");
  svg.selectAll("*").remove();
  if (!W.length) return;
  const width = svg.node().getBoundingClientRect().width, height = 150, m = { t: 8, r: 8, b: 22, l: 30 };
  const byYear = d3.rollup(W, (v) => v.length, (w) => w.year);
  const a = +$("#f-from").value, b = +$("#f-to").value;
  const yrs = d3.range(Math.max(Math.min(a, b), d3.min(W, (w) => w.year)), Math.min(Math.max(a, b), d3.max(W, (w) => w.year)) + 1);
  const x = d3.scaleBand().domain(yrs).range([m.l, width - m.r]).paddingInner(0.15);
  const y = d3.scaleLinear().domain([0, d3.max(byYear.values())]).nice().range([height - m.b, m.t]);
  const yTicks = y.ticks(Math.min(4, y.domain()[1])).filter(Number.isInteger);
  svg.append("g").attr("class", "grid").attr("transform", `translate(${m.l},0)`)
    .call(d3.axisLeft(y).tickValues(yTicks).tickSize(-(width - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`)
    .call(d3.axisLeft(y).tickValues(yTicks).tickFormat(d3.format("d")).tickSize(0).tickPadding(6)).select(".domain").remove();
  const every = Math.ceil(yrs.length / Math.max(2, Math.floor(width / 48)));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${height - m.b})`)
    .call(d3.axisBottom(x).tickValues(yrs.filter((yy, i) => i % every === 0)).tickSize(0).tickPadding(6));
  const bw = Math.max(1, x.bandwidth()), rad = Math.min(4, bw / 2);
  svg.append("g").selectAll("path").data(yrs.filter((yy) => byYear.get(yy)))
    .join("path").attr("fill", "var(--seq-bar)")
    .attr("d", (yy) => {
      const x0 = x(yy), y0 = y(byYear.get(yy)), y1 = y(0), rr = Math.min(rad, y1 - y0);
      return `M${x0},${y1}V${y0 + rr}Q${x0},${y0} ${x0 + rr},${y0}H${x0 + bw - rr}Q${x0 + bw},${y0} ${x0 + bw},${y0 + rr}V${y1}Z`;
    });
  svg.append("g").selectAll("rect").data(yrs).join("rect").attr("class", "hit")
    .attr("x", (yy) => x(yy) - (x.step() - bw) / 2).attr("width", x.step()).attr("y", m.t).attr("height", height - m.b - m.t)
    .on("mousemove", (e, yy) => showTip(e, `<b>${yy}</b>${byYear.get(yy) || 0} papers`)).on("mouseleave", hideTip);
}

/* ------------------------------------------------------------------ SEARCH */
let searchLimit = 100;
function initSearch() {
  $("#s-fac").insertAdjacentHTML("beforeend", S.meta.faculty.map((f) => `<option value="${f.slug}">${esc(f.name)}</option>`).join(""));
  let t;
  $("#s-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { searchLimit = 100; renderSearch(); }, 150); });
  $("#s-fac").addEventListener("change", () => { searchLimit = 100; renderSearch(); });
  $("#s-more").addEventListener("click", () => { searchLimit += 200; renderSearch(); });
  $("#s-csv").addEventListener("click", downloadCsv);
}
function searchResults() {
  const terms = $("#s-q").value.toLowerCase().split(/\s+/).filter(Boolean);
  const fac = $("#s-fac").value;
  return S.filtered.filter((w) => (!fac || w.faculty.includes(fac)) && terms.every((t) => w.text.includes(t)));
}
function renderSearch() {
  const R = searchResults();
  $("#s-count").textContent = `${fmt(R.length)} papers`;
  $("#s-table tbody").innerHTML = R.slice(0, searchLimit).map((w) => `<tr>
    <td class="y">${w.year}</td>
    <td><a href="${doiLink(w)}" target="_blank" rel="noopener">${esc(w.title)}</a>${w.venue ? ` <span class="muted">· ${esc(w.venue)}</span>` : ""}
      ${w.summary ? `<div class="sum">${esc(w.summary)}</div>` : ""}</td>
    <td>${w.faculty.map((s) => `<a href="#faculty/${s}">${esc(S.fac.get(s)?.name)}</a>`).join(", ")}</td>
    <td>${esc(S.theme.get(w.theme)?.name ?? "")}</td></tr>`).join("");
  $("#s-more").hidden = R.length <= searchLimit;
}
function downloadCsv() {
  const R = searchResults();
  const cols = ["year", "title", "venue", "doi", "faculty", "theme", "subtopics", "partner_orgs", "taxa", "species", "regions", "summary", "openalex_id"];
  const cell = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [cols.join(",")].concat(R.map((w) => [w.year, w.title, w.venue, w.doi,
    w.faculty.map((s) => S.fac.get(s)?.name).join("; "), S.theme.get(w.theme)?.name,
    w.subtopics.map((s) => S.sub.get(s)?.name).join("; "), w.orgs.map(orgName).join("; "),
    w.taxa.join("; "), w.species.join("; "), w.regions.join("; "), w.summary, w.id].map(cell).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  a.download = "safs-papers.csv";
  a.click();
  URL.revokeObjectURL(a.href);
}
