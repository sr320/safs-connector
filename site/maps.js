/* M6: collaborator (world) map and theme map. Uses the shared state and helpers in app.js. */
"use strict";

const WORLD_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json";
const SEATTLE = [-122.31, 47.65];
const M = {};   // collaborator map state
const T = {};   // theme map state
const regionNames = (() => { try { return new Intl.DisplayNames(["en"], { type: "region" }); } catch { return null; } })();
const countryName = (cc) => { try { return (cc && regionNames?.of(cc)) || cc || "Unknown"; } catch { return cc; } };
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function initMaps() {
  const opts = S.meta.faculty.map((f) => `<option value="${f.slug}">${esc(f.name)}</option>`).join("");
  ["#m-fac", "#t-a", "#t-b"].forEach((s) => $(s).insertAdjacentHTML("beforeend", opts));
  $("#m-fac").addEventListener("change", () => { M.sel = null; renderMap(); });
  $("#t-a").addEventListener("change", renderThemeMap);
  $("#t-b").addEventListener("change", renderThemeMap);
  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { M.built = false; T.ready = false; renderAll(); }, 200);
  });
}

/* ------------------------------------------------------------------ collaborator map */
async function renderMap() {
  if (!M.world) {
    M.loading ||= d3.json(WORLD_URL).then((w) => { M.world = w; });
    try { await M.loading; } catch (e) {
      $("#map-panel").innerHTML = `<p>Could not load the world outline (${esc(e.message)}).</p>`;
      return;
    }
  }
  const svg = d3.select("#map");
  const { width, height } = svg.node().getBoundingClientRect();
  if (!width) return;
  if (!M.built) {
    svg.selectAll("*").remove();
    const land = topojson.feature(M.world, M.world.objects.countries);
    M.proj = d3.geoNaturalEarth1().fitExtent([[8, 8], [width - 8, height - 8]], { type: "Sphere" });
    const path = d3.geoPath(M.proj);
    M.g = svg.append("g");
    M.g.append("path").attr("class", "sphere").attr("d", path({ type: "Sphere" }));
    M.g.append("g").selectAll("path").data(land.features).join("path").attr("class", "land").attr("d", path);
    M.gb = M.g.append("g");
    const [hx, hy] = M.proj(SEATTLE);
    M.home = M.g.append("g");
    M.home.append("circle").attr("class", "home").attr("cx", hx).attr("cy", hy).attr("r", 6);
    M.home.append("text").attr("class", "home-lab").attr("x", hx - 9).attr("y", hy).attr("dy", "0.32em").attr("text-anchor", "end").text("SAFS");
    M.k = 1;
    svg.call(d3.zoom().scaleExtent([1, 12]).translateExtent([[0, 0], [width, height]]).on("zoom", (e) => {
      M.k = e.transform.k;
      M.g.attr("transform", e.transform);
      styleBubbles();
    }));
    svg.on("click", (e) => { if (e.target.tagName !== "circle") { M.sel = null; mapPanel(); styleBubbles(); } });
    M.built = true;
  }

  const fac = $("#m-fac").value;
  const W = S.filtered.filter((w) => !fac || w.faculty.includes(fac));
  const per = new Map();
  for (const w of W) for (const o of w.orgs) {
    const org = S.org[o];
    if (!org || org.lat == null) continue;
    let d = per.get(o);
    if (!d) per.set(o, d = { id: o, org, works: [], xy: M.proj([org.lon, org.lat]) });
    d.works.push(w);
  }
  M.data = [...per.values()].filter((d) => d.xy).sort((a, b) => b.works.length - a.works.length);
  M.r = d3.scaleSqrt().domain([0, d3.max(M.data, (d) => d.works.length) || 1]).range([0, Math.min(30, width / 30)]);
  const countries = new Set(M.data.map((d) => d.org.country));
  $("#m-count").textContent = `${fmt(M.data.length)} partner organizations in ${fmt(countries.size)} countries`;

  M.gb.selectAll("circle").data(M.data, (d) => d.id).join("circle")
    .attr("class", "bub")
    .attr("cx", (d) => d.xy[0]).attr("cy", (d) => d.xy[1])
    .on("mousemove", (e, d) => showTip(e, `<b>${esc(d.org.name)}</b>${esc([d.org.city, countryName(d.org.country)].filter(Boolean).join(", "))}<br>${fmt(d.works.length)} shared papers · ${new Set(d.works.flatMap((w) => w.faculty)).size} SAFS faculty`))
    .on("mouseleave", hideTip)
    .on("click", (e, d) => { e.stopPropagation(); M.sel = d.id; mapPanel(); styleBubbles(); })
    .order();
  M.home.raise();
  styleBubbles();
  mapPanel();
}

function styleBubbles() {
  const s = 1 / Math.sqrt(M.k);
  M.gb.selectAll("circle")
    .attr("r", (d) => Math.max(2, M.r(d.works.length)) * s)
    .attr("stroke-width", (d) => (d.id === M.sel ? 2 : 0.75) / M.k)
    .classed("sel", (d) => d.id === M.sel);
  M.home.select("circle").attr("r", 6 / M.k).attr("stroke-width", 2 / M.k);
  M.home.select("text").attr("font-size", 11 / M.k).attr("stroke-width", 3 / M.k)
    .attr("x", M.proj(SEATTLE)[0] - 9 / M.k);
}

function mapPanel() {
  const panel = $("#map-panel");
  const d = M.data.find((x) => x.id === M.sel);
  if (!d) {
    const byCountry = topN(countBy(M.data, (x) => x.org.country), 12);
    panel.innerHTML = `<p class="muted">Each circle is a partner organization; its area shows how many papers it shares with SAFS faculty. Click one for the papers. Scroll to zoom, drag to pan.</p>
      <h3>Top partners</h3>${barList(M.data.slice(0, 10).map((x) => [x.id, x.works.length]), { label: orgName })}
      <h3 style="margin-top:14px">Partner organizations by country</h3>${barList(byCountry, { label: countryName })}`;
    return;
  }
  const fac = countBy(d.works, (w) => w.faculty);
  panel.innerHTML = `<div class="kind">Partner organization</div><h2>${esc(d.org.name)}</h2>
    <p class="muted">${esc([d.org.city, countryName(d.org.country)].filter(Boolean).join(", "))} · ${esc(d.org.type ?? "")} · ${fmt(d.works.length)} shared papers</p>
    <h3>SAFS faculty</h3><div id="mp-fac">${barList(topN(fac, 15), { label: (k) => S.fac.get(k)?.name ?? k, onClick: true })}</div>
    <h3 style="margin-top:14px">Papers</h3>${paperList(d.works, 50)}`;
  panel.querySelectorAll("#mp-fac button").forEach((b) => b.addEventListener("click", () => openFaculty(b.dataset.k)));
}

/* ------------------------------------------------------------------ theme map */
async function renderThemeMap() {
  if (!T.xy) {
    T.loading ||= d3.json("data/theme_map.json?v=" + window.V).then((d) => { T.xy = new Map(d.rows.map((r) => [r[0], [r[1], r[2]]])); });
    await T.loading;
  }
  if (!T.ready) setupThemeCanvas();
  if (!T.ready) return;
  const a = $("#t-a").value, b = $("#t-b").value;
  T.pts = [];
  for (const w of S.filtered) {
    const xy = T.xy.get(w.id);
    if (!xy) continue;
    const inA = !!a && w.faculty.includes(a), inB = !!b && w.faculty.includes(b);
    T.pts.push({ w, x: T.sx(xy[0]), y: T.sy(xy[1]), cls: inA && inB ? "both" : inA ? "a" : inB ? "b" : "o" });
  }
  // draw highlighted papers last so they sit on top
  const order = { o: 0, b: 1, a: 2, both: 3 };
  T.pts.sort((p, q) => order[p.cls] - order[q.cls]);
  T.labels = d3.groups(T.pts.filter((p) => p.w.theme), (p) => p.w.theme)
    .filter(([, ps]) => ps.length >= 12)
    .sort((p, q) => q[1].length - p[1].length)
    .map(([t, ps]) => ({ text: S.theme.get(t).name, x: d3.median(ps, (p) => p.x), y: d3.median(ps, (p) => p.y) }));
  T.qt = d3.quadtree(T.pts, (p) => p.x, (p) => p.y);
  const n = (c) => T.pts.filter((p) => p.cls === c).length;
  const sw = (v) => `<i class="swatch" style="background:var(${v})"></i>`;
  $("#t-legend").innerHTML = [
    a && `<span>${sw("--series-1")}${esc(S.fac.get(a).name)} (${n("a")})</span>`,
    b && `<span>${sw("--series-2")}${esc(S.fac.get(b).name)} (${n("b")})</span>`,
    a && b && `<span>${sw("--series-3")}Both (${n("both")})</span>`,
    `<span>${sw("--muted")}${a || b ? "Other papers" : "All papers"} (${n("o")})</span>`,
  ].filter(Boolean).join("");
  drawThemeMap();
}

function setupThemeCanvas() {
  const canvas = $("#tmap");
  const box = canvas.parentElement.getBoundingClientRect();
  if (!box.width) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = box.width * dpr; canvas.height = box.height * dpr;
  T.ctx = canvas.getContext("2d");
  T.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  T.w = box.width; T.h = box.height;
  const pad = 24;
  T.sx = d3.scaleLinear().domain([0, 1]).range([pad, T.w - pad]);
  T.sy = d3.scaleLinear().domain([0, 1]).range([T.h - pad, pad]);
  T.t = d3.zoomIdentity;
  d3.select(canvas).call(d3.zoom().scaleExtent([1, 16]).translateExtent([[0, 0], [T.w, T.h]])
    .on("zoom", (e) => { T.t = e.transform; drawThemeMap(); }));
  d3.select(canvas)
    .on("mousemove", (e) => {
      const [mx, my] = d3.pointer(e);
      const [x, y] = T.t.invert([mx, my]);
      const p = T.qt?.find(x, y, 10 / T.t.k);
      if (p !== T.hover) { T.hover = p; drawThemeMap(); }
      if (p) {
        showTip(e, `<b>${esc(p.w.title)}</b>${p.w.year} · ${esc(S.theme.get(p.w.theme)?.name ?? "")}<br>${p.w.faculty.map((s) => esc(S.fac.get(s)?.name)).join(", ")}`);
        canvas.style.cursor = "pointer";
      } else { hideTip(); canvas.style.cursor = "crosshair"; }
    })
    .on("mouseleave", () => { T.hover = null; hideTip(); drawThemeMap(); })
    .on("click", () => { if (T.hover) window.open(doiLink(T.hover.w), "_blank", "noopener"); });
  T.ready = true;
}

function drawThemeMap() {
  const c = T.ctx;
  if (!c || !T.pts) return;
  const col = { o: cssVar("--muted"), a: cssVar("--series-1"), b: cssVar("--series-2"), both: cssVar("--series-3") };
  const ring = cssVar("--surface-1");
  const anyHi = T.pts.some((p) => p.cls !== "o");
  c.clearRect(0, 0, T.w, T.h);
  c.save();
  c.translate(T.t.x, T.t.y);
  c.scale(T.t.k, T.t.k);
  const k = T.t.k;
  for (const p of T.pts) {
    const hi = p.cls !== "o";
    const r = (hi ? 3.4 : 2.2) / Math.sqrt(k);
    c.globalAlpha = hi ? 1 : anyHi ? 0.22 : 0.45;
    c.beginPath();
    c.arc(p.x, p.y, r, 0, 2 * Math.PI);
    c.fillStyle = col[p.cls];
    c.fill();
    if (hi) { c.lineWidth = 1 / k; c.strokeStyle = ring; c.stroke(); }
  }
  c.globalAlpha = 1;
  if (T.hover) {
    c.beginPath();
    c.arc(T.hover.x, T.hover.y, 6 / Math.sqrt(k), 0, 2 * Math.PI);
    c.lineWidth = 2 / k;
    c.strokeStyle = cssVar("--text-primary");
    c.stroke();
  }
  c.restore();
  // theme labels in screen space so text stays the same size when zooming
  c.font = `600 12px system-ui, -apple-system, "Segoe UI", sans-serif`;
  c.textAlign = "center";
  c.textBaseline = "middle";
  c.lineJoin = "round";
  const placed = [];
  const hit = (b) => placed.some((q) => b.x0 < q.x1 && b.x1 > q.x0 && b.y0 < q.y1 && b.y1 > q.y0);
  for (const l of T.labels) {
    let [x, y] = T.t.apply([l.x, l.y]);
    if (x < 0 || x > T.w || y < 0 || y > T.h) continue;
    const w = c.measureText(l.text).width + 6, h = 15;
    x = Math.max(w / 2 + 2, Math.min(T.w - w / 2 - 2, x));   // keep the whole label inside the canvas
    // nudge up/down until the label no longer overlaps one already drawn
    for (const dy of [0, -h, h, -2 * h, 2 * h, -3 * h, 3 * h]) {
      const b = { x0: x - w / 2, x1: x + w / 2, y0: y + dy - h / 2, y1: y + dy + h / 2 };
      if (!hit(b)) { y += dy; placed.push(b); break; }
    }
    c.lineWidth = 4;
    c.strokeStyle = ring;
    c.strokeText(l.text, x, y);
    c.fillStyle = cssVar("--text-secondary");
    c.fillText(l.text, x, y);
  }
}
