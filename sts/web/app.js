/* Steer the Story — front-end. Vanilla JS, no build step. Works served by `sts serve` and,
   for reading only, when index.html is opened straight from disk. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const served = location.protocol.startsWith("http");
const LS = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
             set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} } };

// ------------------------------------------------------------------ tabs
const views = { play: $("#view-play"), compile: $("#view-compile") };
let playOnly = !served;
function showTab(name) {
  if (name !== "play" && name !== "compile") name = "play";
  if (name === "compile" && playOnly) name = "play";
  for (const [k, v] of Object.entries(views)) v.hidden = k !== name;
  $$(".tabs a").forEach(a => a.setAttribute("aria-selected", a.dataset.tab === name ? "true" : "false"));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (v != null) e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
}
function fmtBytes(n) { return n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.round(n / 1e3) + " kB"; }
function fmtDur(s) { s = Math.round(s); return s < 60 ? s + "s" : s < 3600 ? Math.floor(s / 60) + "m " + (s % 60) + "s" : Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m"; }
async function readAdventureFile(file) {
  let buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
    if (!("DecompressionStream" in window)) throw new Error("this browser cannot open .gz files; use the plain .sts");
    const ds = new DecompressionStream("gzip");
    buf = await new Response(new Blob([buf]).stream().pipeThrough(ds)).arrayBuffer();
  }
  const data = JSON.parse(new TextDecoder().decode(buf));
  if (data.format !== "sts/1") throw new Error("this is not a Steer the Story adventure file");
  return data;
}

// ------------------------------------------------------------------ reader
const R = { adv: null, key: "", cur: null, hist: [], found: new Set(), canonOrder: [], canonPos: {}, choicePoints: new Set(),
            branchInfo: {}, totalEndings: 0 };
const reader = $("#reader"), playEmpty = $("#play-empty");

function analyseGraph(adv) {
  const nodes = adv.nodes;
  // canon spine: follow canon-flagged (or single) choices from start
  const order = []; const pos = {}; let id = adv.start; const seen = new Set();
  while (id && nodes[id] && !seen.has(id)) {
    seen.add(id); pos[id] = order.length; order.push(id);
    const n = nodes[id]; const ch = n.choices || [];
    const next = ch.find(c => c.canon) || (ch.length === 1 ? ch[0] : null);
    id = next ? next.to : null;
  }
  const cps = new Set(order.filter(i => (nodes[i].choices || []).length > 1));
  // branch info: origin canon index + rejoin index (or -1 for ending)
  const binfo = {};
  for (const [nid, n] of Object.entries(nodes)) {
    if (!n.branch_id) continue;
    if (!binfo[n.branch_id]) binfo[n.branch_id] = { origin: -1, rejoin: -1, ending: false, nodes: [] };
    binfo[n.branch_id].nodes.push(nid);
    if (n.kind === "ending") binfo[n.branch_id].ending = true;
    for (const c of (n.choices || [])) if (pos[c.to] != null) binfo[n.branch_id].rejoin = pos[c.to];
  }
  for (const cid of order) for (const c of (nodes[cid].choices || [])) {
    const t = nodes[c.to]; if (t && t.branch_id && binfo[t.branch_id]) binfo[t.branch_id].origin = pos[cid];
  }
  R.canonOrder = order; R.canonPos = pos; R.choicePoints = cps; R.branchInfo = binfo;
  R.totalEndings = Object.values(nodes).filter(n => n.kind === "ending" || !(n.choices || []).length).length;
}

function openAdventure(adv, sourceName) {
  R.adv = adv; R.key = "sts:" + (adv.meta.source_sha256 || adv.meta.title || sourceName || "x").slice(0, 24) + ":" + (adv.meta.created || "");
  analyseGraph(adv);
  const saved = LS.get(R.key, null);
  R.found = new Set((saved && saved.found) || []);
  R.hist = (saved && saved.hist && saved.hist.every(h => adv.nodes[h])) ? saved.hist : [];
  R.cur = (saved && adv.nodes[saved.cur]) ? saved.cur : adv.start;
  $("#book-title").textContent = adv.meta.title || "Untitled";
  $("#book-byline").textContent = (adv.meta.author ? "by " + adv.meta.author + " · " : "") + "steered by you";
  playEmpty.hidden = true; reader.hidden = false;
  showTab("play");
  render(false);
}
function persist() { LS.set(R.key, { cur: R.cur, hist: R.hist.slice(-400), found: [...R.found] }); }
function closeBook() { R.adv = null; reader.hidden = true; playEmpty.hidden = false; }

function render(animate = true) {
  const adv = R.adv, n = adv.nodes[R.cur];
  if (!n) { R.cur = adv.start; return render(false); }
  const isEnding = n.kind === "ending" || !(n.choices || []).length;
  if (isEnding && !R.found.has(R.cur)) R.found.add(R.cur);
  persist();
  // eyebrow
  const eb = $("#eyebrow"); eb.innerHTML = "";
  const showOrigin = $("#show-origin").checked;
  if (n.chapter_title) eb.append(el("span", {}, n.chapter_title));
  else if (n.branch_id) { const bi = R.branchInfo[n.branch_id]; const oc = bi && bi.origin >= 0 ? adv.nodes[R.canonOrder[bi.origin]] : null;
    eb.append(el("span", {}, oc && oc.chapter_title ? oc.chapter_title + " · another way" : "another way")); }
  if (showOrigin) {
    if (n.branch_id) eb.append(el("span", { class: "tag detour", title: "This passage was written for this edition by the language model" }, "new passage"));
    else eb.append(el("span", { class: "tag canon", title: "This passage is the author's original text" }, "original text"));
  }
  if (isEnding) eb.append(el("span", { class: "tag ending" }, "an ending"));
  // passage
  const p = $("#passage"); p.innerHTML = "";
  p.className = "passage" + (n.branch_id ? " detour" : "") + (showOrigin ? "" : " plain") + (animate ? " turn" : "");
  for (const para of n.text.split(/\n\s*\n/)) { const t = para.trim(); if (t) p.append(paragraph(t)); }
  // choices
  const c = $("#choices"); c.innerHTML = "";
  if (isEnding) {
    const isOriginal = !n.branch_id;
    c.append(el("div", { class: "the-end" },
      el("div", { class: "fin" }, "The End"),
      el("h2", {}, n.ending_title || (isOriginal ? "The original ending" : "An ending")),
      el("p", { class: "sub" }, `You have found ${R.found.size} of ${R.totalEndings} endings.` + (isOriginal ? " This is how the book itself ends." : "")),
      el("div", { class: "row", style: "justify-content:center" },
        el("button", { class: "ghost", onclick: () => backToChoice() }, "Return to your last choice"),
        el("button", { class: "ghost", onclick: restart }, "Start over"))));
  } else if (n.choices.length === 1) {
    c.append(el("button", { class: "choice single", onclick: () => go(n.choices[0].to) }, el("span", { class: "label" }, "Continue"), el("span", { class: "arrow" }, "→")));
  } else {
    c.append(el("p", { class: "q" }, n.question || "What happens next?"));
    n.choices.forEach((ch, i) => c.append(el("button", { class: "choice", onclick: () => go(ch.to) },
      el("span", { class: "num" }, String(i + 1)), el("span", { class: "label" }, ch.label), el("span", { class: "arrow" }, "→"))));
  }
  $("#btn-back").disabled = !R.hist.length;
  $("#endings-found").textContent = `${R.found.size} / ${R.totalEndings} endings found`;
  drawRoute();
  window.scrollTo({ top: 0, behavior: animate ? "smooth" : "auto" });
}
// Gutenberg-style _underscores_ mark italics; render them as <em> without trusting any HTML.
function paragraph(text) {
  const p = el("p");
  const lines = text.split("\n").map(l => l.trim()).filter(Boolean);
  // Hard-wrapped prose (Gutenberg) is re-flowed; verse (short lines, capitalised starts) keeps its line breaks.
  const isVerse = lines.length >= 2 && lines.every(l => l.length < 60) && lines.filter(l => /^["'“‘(]?[A-Z]/.test(l)).length >= lines.length * 0.8;
  if (isVerse) p.classList.add("verse");
  (isVerse ? lines : [lines.join(" ")]).forEach((line, li) => {
    if (li) p.append(el("br"));
    // _underscores_ (Gutenberg) and *asterisks* (models) both mean italics
    let last = 0; const re = /_([^_\n]{1,200}?)_|\*([^*\n]{1,200}?)\*/g; let m;
    while ((m = re.exec(line))) { if (m.index > last) p.append(document.createTextNode(line.slice(last, m.index))); p.append(el("em", {}, m[1] || m[2])); last = re.lastIndex; }
    if (last < line.length) p.append(document.createTextNode(line.slice(last)));
  });
  return p;
}
function go(to) { if (!R.adv.nodes[to]) return; R.hist.push(R.cur); R.cur = to; render(true); }
function back() { if (!R.hist.length) return; R.cur = R.hist.pop(); render(true); }
function backToChoice() {
  while (R.hist.length) { const id = R.hist.pop(); if ((R.adv.nodes[id].choices || []).length > 1) { R.cur = id; return render(true); } }
  R.cur = R.adv.start; render(true);
}
function restart() { R.hist = []; R.cur = R.adv.start; render(true); }

// route strip: the book's spine with your position; a detour arcs above it.
function drawRoute() {
  const svg = $("#route"); const W = Math.max(300, svg.clientWidth || 600), H = 44; svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const N = R.canonOrder.length; if (!N) { svg.innerHTML = ""; return; }
  const x = i => 8 + (W - 16) * (N === 1 ? 0 : i / (N - 1)); const y = 30;
  const visited = new Set([...R.hist, R.cur].filter(id => R.canonPos[id] != null).map(id => R.canonPos[id]));
  let s = `<line x1="${x(0)}" y1="${y}" x2="${x(N - 1)}" y2="${y}" stroke="var(--rule)" stroke-width="2"/>`;
  // choice points as ticks; visited stretch in ink
  const maxVisited = Math.max(-1, ...visited);
  if (maxVisited >= 0) s += `<line x1="${x(0)}" y1="${y}" x2="${x(maxVisited)}" y2="${y}" stroke="var(--canon)" stroke-width="2"/>`;
  for (const cid of R.choicePoints) { const i = R.canonPos[cid]; s += `<line x1="${x(i)}" y1="${y - 5}" x2="${x(i)}" y2="${y + 5}" stroke="${i <= maxVisited ? "var(--canon)" : "var(--rule)"}" stroke-width="2"/>`; }
  // detours taken (from history) and the current one
  const arcs = new Map();
  for (const id of [...R.hist, R.cur]) { const b = R.adv.nodes[id] && R.adv.nodes[id].branch_id; if (b && R.branchInfo[b]) arcs.set(b, id === R.cur); }
  const cur = R.adv.nodes[R.cur];
  for (const [b, active] of arcs) {
    const bi = R.branchInfo[b]; if (bi.origin < 0) continue;
    const x0 = x(bi.origin), x1 = bi.rejoin >= 0 ? x(bi.rejoin) : x0 + Math.max(24, (W - 16) / Math.max(N - 1, 1) * 2);
    const col = "var(--detour)";
    if (bi.rejoin >= 0) s += `<path d="M ${x0} ${y} C ${x0} ${y - 26}, ${x1} ${y - 26}, ${x1} ${y}" fill="none" stroke="${col}" stroke-width="2" ${active ? "" : 'opacity=".55"'}/>`;
    else s += `<path d="M ${x0} ${y} C ${x0} ${y - 24}, ${x1 - 6} ${y - 24}, ${x1} ${y - 14}" fill="none" stroke="${col}" stroke-width="2" ${active ? "" : 'opacity=".55"'}/><circle cx="${x1}" cy="${y - 14}" r="3" fill="var(--ending)"/>`;
  }
  // position marker
  let px, py;
  if (R.canonPos[R.cur] != null) { px = x(R.canonPos[R.cur]); py = y; }
  else if (cur.branch_id && R.branchInfo[cur.branch_id]) {
    const bi = R.branchInfo[cur.branch_id]; const k = bi.nodes.indexOf(R.cur), L = bi.nodes.length;
    const t = (k + 1) / (L + 1); const x0 = x(bi.origin), x1 = bi.rejoin >= 0 ? x(bi.rejoin) : x0 + Math.max(24, (W - 16) / Math.max(N - 1, 1) * 2);
    // point on the cubic
    const mt = 1 - t; const cy = bi.rejoin >= 0 ? y - 26 : y - 24;
    px = mt*mt*mt*x0 + 3*mt*mt*t*x0 + 3*mt*t*t*x1 + t*t*t*x1; py = mt*mt*mt*y + 3*mt*mt*t*cy + 3*mt*t*t*cy + t*t*t*(bi.rejoin >= 0 ? y : y - 14);
  } else { px = x(0); py = y; }
  s += `<circle cx="${px}" cy="${py}" r="5" fill="var(--paper)" stroke="${cur.branch_id ? "var(--detour)" : "var(--canon)"}" stroke-width="2.5"/>`;
  s += `<text x="${x(0)}" y="${H - 1}" font-family="var(--sans)" font-size="10" fill="var(--muted)">start</text><text x="${x(N-1)}" y="${H - 1}" text-anchor="end" font-family="var(--sans)" font-size="10" fill="var(--muted)">the end</text>`;
  svg.innerHTML = s;
}

window.addEventListener("resize", () => { if (R.adv && !reader.hidden) drawRoute(); });
$("#btn-back").addEventListener("click", back);
$("#btn-restart").addEventListener("click", restart);
$("#btn-close").addEventListener("click", closeBook);
$("#show-origin").addEventListener("change", () => render(false));
document.addEventListener("keydown", e => {
  if (!R.adv || reader.hidden || e.target.matches("input,textarea")) return;
  if (e.key === "Backspace") { e.preventDefault(); back(); }
  const k = parseInt(e.key, 10); if (k >= 1 && k <= 9) { const b = $$("#choices .choice")[k - 1]; if (b) b.click(); }
  if (e.key === "Enter" || e.key === " ") { const s = $("#choices .choice.single"); if (s) { e.preventDefault(); s.click(); } }
});

// drop / pick .sts
function wireDrop(zone, onFile) {
  ["dragenter", "dragover"].forEach(t => zone.addEventListener(t, e => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach(t => zone.addEventListener(t, e => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", e => { e.stopPropagation(); const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) onFile(f); });
}
async function loadStsFile(f) {
  try { openAdventure(await readAdventureFile(f), f.name); }
  catch (e) { alert("Could not open " + f.name + ": " + e.message); }
}
wireDrop(playEmpty, loadStsFile);
$("#sts-file").addEventListener("change", e => { if (e.target.files[0]) loadStsFile(e.target.files[0]); e.target.value = ""; });
document.body.addEventListener("dragover", e => e.preventDefault());
document.body.addEventListener("drop", e => { e.preventDefault(); const f = e.dataTransfer.files && e.dataTransfer.files[0]; if (!f) return;
  if (/\.(sts|gz|json)$/i.test(f.name)) loadStsFile(f); else if (!playOnly) { showTab("compile"); uploadBook(f); } });

async function openFromLibrary(name) {
  try { const r = await fetch("/api/library/" + encodeURIComponent(name)); if (!r.ok) throw new Error(r.statusText);
    openAdventure(await readAdventureFile(await r.blob()), name); }
  catch (e) { alert("Could not open " + name + ": " + e.message); }
}
function renderLibrary(items) {
  for (const list of [$("#library-list"), $("#library-list-2")]) {
    list.innerHTML = "";
    if (!items.length) { if (list.id === "library-list-2") list.append(el("p", { class: "muted" }, "Nothing here yet — make one on the left.")); continue; }
    for (const it of items) list.append(el("div", { class: "lib-item" },
      el("a", { class: "t", href: "#play", onclick: e => { e.preventDefault(); openFromLibrary(it.name); } }, it.title || it.name),
      el("span", { class: "m" }, [it.author, it.endings != null ? it.endings + " endings" : null, it.model, fmtBytes(it.size)].filter(Boolean).join(" · ")),
      el("span", { class: "actions" }, el("a", { href: "/api/library/" + encodeURIComponent(it.name), download: it.name }, "download"),
        el("button", { onclick: async () => { if (confirm("Delete " + it.name + "?")) { await api("/api/library/" + encodeURIComponent(it.name), { method: "DELETE" }); refreshStatus(); } } }, "delete"))));
  }
}

// ------------------------------------------------------------------ compile
const form = $("#cfg-form"); let upload = null; let estimateTimer = null;
const F = name => form.elements[name];
function cfgFromForm() {
  const c = { llm: { base_url: F("base_url").value.trim(), model: F("model").value.trim(), api_key: F("api_key").value } };
  for (const k of ["choice_every", "branches", "branch_len", "rejoin_after", "ending_ratio", "branch_scene_words", "scene_tokens", "concurrency", "chapters"]) c[k] = F(k).value;
  return c;
}
function saveLLMSettings() { LS.set("sts:llm", { base_url: F("base_url").value, model: F("model").value, api_key: F("api_key").value, concurrency: F("concurrency").value }); }
function loadLLMSettings(defaults) {
  const s = LS.get("sts:llm", {});
  F("base_url").value = s.base_url || defaults.base_url || ""; F("model").value = s.model || defaults.model || "";
  F("api_key").value = s.api_key || ""; F("concurrency").value = s.concurrency || 1;
}
form.addEventListener("input", () => { saveLLMSettings(); scheduleEstimate(); });
$("#btn-probe").addEventListener("click", async () => {
  const out = $("#probe-result"); out.textContent = "testing…"; out.className = "muted";
  try { const r = await api("/api/probe", { method: "POST", body: JSON.stringify(cfgFromForm().llm) });
    if (r.ok) { out.textContent = `OK — answered in ${r.latency_s}s` + (r.json_mode ? ", JSON mode supported" : ", plain-text JSON") + (r.warning ? " · " + r.warning : ""); out.className = "ok"; }
    else { out.textContent = "Failed: " + (r.error || JSON.stringify(r)); out.className = "bad"; } }
  catch (e) { out.textContent = "Failed: " + e.message; out.className = "bad"; }
});
wireDrop($("#book-drop"), uploadBook);
$("#book-file").addEventListener("change", e => { if (e.target.files[0]) uploadBook(e.target.files[0]); e.target.value = ""; });
async function uploadBook(file) {
  const info = $("#book-info"); info.hidden = false; info.innerHTML = ""; info.append(el("p", { class: "muted" }, "Reading " + file.name + "…"));
  $("#btn-start").disabled = true; upload = null;
  try {
    const r = await fetch("/api/upload", { method: "POST", headers: { "X-Filename": file.name, "Content-Type": "application/octet-stream" }, body: file });
    const data = await r.json(); if (!r.ok) throw new Error(data.error || r.statusText);
    upload = data;
    info.innerHTML = "";
    info.append(el("div", { class: "title" }, data.title || file.name), el("div", { class: "muted" }, [data.author, `${data.chapters} chapters`, `${data.words.toLocaleString()} words`].filter(Boolean).join(" · ")));
    if (data.chapter_titles && data.chapter_titles.length > 1) {
      const d = el("details", {}, el("summary", {}, "Chapters found")); const ol = el("ol", { style: "margin:.4rem 0 0 1.2rem;color:var(--muted)" });
      data.chapter_titles.forEach(t => ol.append(el("li", {}, t || "(untitled)"))); d.append(ol); info.append(d);
    }
    $("#btn-start").disabled = false; scheduleEstimate();
  } catch (e) { info.innerHTML = ""; info.append(el("p", { class: "bad" }, "Could not read this file: " + e.message)); }
}
function scheduleEstimate() { clearTimeout(estimateTimer); if (upload) estimateTimer = setTimeout(estimate, 350); }
async function estimate() {
  if (!upload) return; const out = $("#estimate");
  try { const r = await api("/api/dryrun", { method: "POST", body: JSON.stringify({ upload: upload.upload, filename: upload.filename, config: cfgFromForm() }) });
    out.textContent = `${r.scenes} scenes → ${r.choice_points} choice points, ${r.branches} detours (${r.generated_scenes} new scenes), ${r.endings} endings. `
      + `About ${r.llm_calls} model calls; the largest prompt is ~${r.context_needed.toLocaleString()} tokens` + (r.context_needed > 7000 ? " — lower “scene size” if your model's context is 8k." : ".");
  } catch (e) { out.textContent = "Estimate failed: " + e.message; }
}
form.addEventListener("submit", async e => {
  e.preventDefault(); if (!upload) return;
  const note = $("#start-note"); note.textContent = "starting…";
  try { const r = await api("/api/compile", { method: "POST", body: JSON.stringify({ upload: upload.upload, filename: upload.filename, config: cfgFromForm() }) });
    note.textContent = ""; watchJob(r.job); }
  catch (err) { note.textContent = err.message; }
});

// jobs
const jobCards = {};
function jobCard(job) {
  let card = jobCards[job.id];
  if (!card) { card = $("#tpl-job").content.firstElementChild.cloneNode(true); jobCards[job.id] = card; $("#jobs").prepend(card); }
  $(".job-title", card).textContent = job.title || job.filename;
  const st = $(".job-status", card); st.textContent = job.status; st.className = "pill " + job.status;
  const pct = job.total ? Math.round(100 * job.done / job.total) : 0;
  $(".bar-fill", card).style.width = (job.status === "done" ? 100 : pct) + "%";
  const phase = { setup: "Learning the author's voice", analyse: "Reading the book", branch: "Designing choices & writing detours", done: "Done" }[job.phase] || "";
  const u = job.usage || {}; const eta = (job.status === "running" && job.done > 2 && job.total > job.done && job.phase !== "setup") ? ` · about ${fmtDur(job.elapsed / job.done * (job.total - job.done))} left` : "";
  $(".job-line", card).textContent = job.status === "error" ? "Error: " + job.error
    : `${phase}${job.total ? ` — ${job.done}/${job.total}` : ""} · ${fmtDur(job.elapsed)}${eta}` + (u.calls ? ` · ${u.calls} calls, ${((u.prompt_tokens || 0) + (u.completion_tokens || 0)).toLocaleString()} tokens` : "") + (job.message && job.status === "running" ? ` · ${job.message}` : "");
  const acts = $(".job-actions", card); acts.innerHTML = "";
  if (job.status === "running" || job.status === "queued") acts.append(el("button", { class: "ghost", onclick: () => api(`/api/jobs/${job.id}/cancel`, { method: "POST" }) }, "Stop (keeps progress)"));
  if (job.status === "done") { acts.append(el("button", { class: "primary", onclick: () => openFromLibrary(job.output) }, "Read it now"), el("a", { href: `/api/jobs/${job.id}/download`, download: job.output }, "Download .sts")); }
  if (job.status === "cancelled") acts.append(el("span", { class: "muted" }, "Progress is saved: drop the same book with the same settings to resume."));
  $(".job-log", card).textContent = (job.log || []).join("\n");
  return card;
}
const watching = new Set();
function watchJob(id) {
  if (watching.has(id)) return; watching.add(id);
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = ev => { const job = JSON.parse(ev.data); jobCard(job); if (["done", "error", "cancelled"].includes(job.status)) { es.close(); watching.delete(id); refreshStatus(); } };
  es.onerror = () => { es.close(); watching.delete(id); setTimeout(() => api(`/api/jobs/${id}`).then(j => { jobCard(j); if (j.status === "running") watchJob(id); }).catch(() => {}), 2000); };
}

async function refreshStatus() {
  if (!served) return;
  try {
    const st = await api("/api/status");
    playOnly = !!st.play_only; $("#tab-compile").hidden = playOnly;
    $("#top-right").textContent = "v" + st.version + (playOnly ? " · reading only" : "");
    renderLibrary(st.library || []);
    if (!playOnly) { if (!refreshStatus.loaded) { loadLLMSettings(st.defaults || {}); refreshStatus.loaded = true; }
      for (const j of st.jobs || []) { jobCard(j); if (j.status === "running" || j.status === "queued") watchJob(j.id); } }
  } catch (e) { $("#top-right").textContent = "server unreachable"; }
}

window.__sts = { uploadBook, watchJob, jobCard, openFromLibrary, refreshStatus, openAdventure };  // for tests/automation

// ------------------------------------------------------------------ boot
(async () => {
  if (!served) { $("#tab-compile").hidden = true; $("#top-right").textContent = "reading only (open via sts serve to make adventures)"; }
  showTab(location.hash.slice(1) || "play");
  if (window.__STS_PRELOAD) { try { openAdventure(window.__STS_PRELOAD, "preload"); } catch (e) { console.error(e); } }
  await refreshStatus();
  const q = new URLSearchParams(location.search).get("open");
  if (q && served && !R.adv) openFromLibrary(q);
  else showTab(location.hash.slice(1) || "play");
})();
})();
