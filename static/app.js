const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const DEFAULT_URLS = { ollama: "http://localhost:11434", lmstudio: "http://localhost:1234/v1" };
let S = { project: { clips: [], suggestions: [], chat: [] }, projects: [], settings: {}, sizes: [], languages: {} };
let plan = { parts: [], duration: 0 }, sel = -1, T = 0, playing = false, mode = "instant", previewUrl = null;
let cur = -1, logCount = 0, wasRunning = false, update = null, peek = false;
const desktop = () => window.pywebview?.api;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmtT = (t) => { t = Math.max(0, t || 0); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = (t % 60).toFixed(1).padStart(4, "0"); return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + s; };
const fmtSize = (b) => (b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(0) + " MB");

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
const post = (path, body, method = "POST") => api(path, { method, body: JSON.stringify(body || {}) });
const fail = (e) => { appendLog("ERROR: " + e.message, true); toast(e.message, true); };
function toast(msg, bad) { $("#stepLabel").textContent = msg; $("#stepLabel").className = "small " + (bad ? "err" : "ok"); }

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== name));
  if (name === "settings") loadSetup();
}
$$(".tab").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
function showPane(btn) {
  const panel = btn.closest(".panel");
  panel.querySelectorAll(".t2").forEach((b) => b.classList.toggle("on", b === btn));
  panel.querySelectorAll(":scope > .pane").forEach((p) => p.classList.toggle("hidden", p.id !== "pane-" + btn.dataset.pane));
}
$$(".t2").forEach((b) => (b.onclick = () => showPane(b)));
const openPane = (name) => showPane($(`.t2[data-pane="${name}"]`));

// ---------------------------------------------------------------- state & settings
async function loadState() {
  S = await api("/api/state");
  if (!$("#whisper_size").options.length) S.sizes.forEach((s) => $("#whisper_size").add(new Option(s, s)));
  if (!$("#language").options.length) Object.entries(S.languages).forEach(([c, n]) => $("#language").add(new Option(n, c)));
  if (!$("#cutStyle").options.length) {
    Object.keys(S.transitions).forEach((t) => $("#cutStyle").add(new Option(t.replace("_", " "), t)));
    Object.keys(S.presets).forEach((p) => $("#preset").add(new Option(p.replace("_", " "), p)));
  }
  $$("[data-setting]").forEach((el) => {
    const v = S.settings[el.dataset.setting];
    if (el.type === "checkbox") el.checked = !!v;
    else if (!["model", "vision_model"].includes(el.id)) el.value = v ?? "";
  });
  $("#target_length").value = S.project.target_length || "";
  $("#brollShareOut").textContent = S.settings.broll_share + "%";
  $("#version").textContent = "v" + S.version;
  $("#defaultOutput").textContent = S.default_output;
  $("#dataDir").textContent = S.data_dir;
  $("#voiceSample").textContent = S.settings.voice_sample || "none";
  $("#voiceConsent").checked = !!S.settings.voice_consent;
  $("#projectSelect").innerHTML = S.projects.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("") + '<option value="__new">+ New project…</option>';
  $("#projectSelect").value = S.project.id;
  renderClips();
  renderTranscript();
  renderLook();
  renderChat();
  renderSuggestions();
  renderProjects();
  await loadPlan();
}

function afterEdit(r) {
  if (r.project) S.project = r.project;
  if ("can_undo" in r) { $("#undoBtn").disabled = !r.can_undo; $("#redoBtn").disabled = !r.can_redo; }
  if (r.done?.length) toast(r.done.join(" · "));
  if (r.errors?.length) appendLog("Skipped: " + r.errors.join("; "), true);
  renderTranscript(); renderLook(); renderSuggestions(); renderChat();
  return loadPlan();
}
const act = (actions, source = "ui") => post("/api/actions", { actions, source }).then(afterEdit).catch(fail);

const saveSettings = (patch) => post("/api/settings", patch).then((s) => (S.settings = s)).catch(fail);
$$("[data-setting]").forEach((el) => el.addEventListener("change", async () => {
  const k = el.dataset.setting;
  const v = el.type === "checkbox" ? el.checked : el.value;
  if (k === "provider") { $("#base_url").value = DEFAULT_URLS[v]; await saveSettings({ provider: v, base_url: DEFAULT_URLS[v] }); return refreshModels(); }
  await saveSettings({ [k]: v });
  if (k === "base_url") refreshModels();
}));
$("#broll_share").addEventListener("input", (e) => ($("#brollShareOut").textContent = e.target.value + "%"));
$("#target_length").addEventListener("change", (e) => act([{ type: "target", value: e.target.value }]));

async function refreshModels() {
  const sel = $("#model"), vis = $("#vision_model"), status = $("#llmStatus");
  status.textContent = "Connecting…";
  status.className = "muted small";
  try {
    const { models } = await api(`/api/llm/models?provider=${encodeURIComponent(S.settings.provider)}&base_url=${encodeURIComponent(S.settings.base_url)}`);
    sel.innerHTML = ""; vis.innerHTML = '<option value="">Off</option>';
    models.forEach((m) => { sel.add(new Option(m, m)); vis.add(new Option(m, m)); });
    if (models.includes(S.settings.model)) sel.value = S.settings.model; else if (models.length) saveSettings({ model: models[0] });
    vis.value = models.includes(S.settings.vision_model) ? S.settings.vision_model : "";
    status.textContent = models.length ? `Connected · ${models.length} model(s)` : "Connected, but no models are loaded/pulled.";
    status.className = "small " + (models.length ? "ok" : "err");
  } catch (e) {
    sel.innerHTML = "";
    status.textContent = e.message;
    status.className = "small err";
  }
  return status.textContent;
}
$("#refreshModels").onclick = refreshModels;

// ---------------------------------------------------------------- projects
$("#projectSelect").onchange = async (e) => {
  try {
    if (e.target.value === "__new") {
      const name = prompt("Name of the new project:", "New ride");
      if (name === null) return ($("#projectSelect").value = S.project.id);
      await post("/api/projects", { name });
    } else await post(`/api/projects/${e.target.value}/open`);
    stop(); T = 0; sel = -1; selIds = new Set(); previewUrl = null; mode = "instant"; syncMode();
    await loadState();
  } catch (err) { fail(err); $("#projectSelect").value = S.project.id; }
};
function renderProjects() {
  $("#projectRows").innerHTML = S.projects.map((p) => `
    <tr data-id="${p.id}" class="${p.id === S.project.id ? "current" : ""}">
      <td><input type="checkbox" class="q" aria-label="Include ${esc(p.name)} in batch"></td>
      <td>${esc(p.name)}</td><td>${p.clips}</td><td>${fmtT(p.duration)}</td>
      <td class="muted">${p.analyzed ? "Edited" : "Not edited yet"}</td>
      <td><button data-act="open">Open</button> <button data-act="rename">Rename</button> <button data-act="delete">Remove</button></td>
    </tr>`).join("");
}
$("#projectRows").onclick = async (e) => {
  const a = e.target.dataset.act, id = e.target.closest("tr")?.dataset.id;
  if (!a) return;
  const p = S.projects.find((x) => x.id === id);
  try {
    if (a === "open") { await post(`/api/projects/${id}/open`); T = 0; sel = -1; await loadState(); showTab("editor"); }
    if (a === "rename") { const name = prompt("New name:", p.name); if (name) { await post(`/api/projects/${id}`, { name }, "PATCH"); await loadState(); } }
    if (a === "delete" && confirm(`Remove “${p.name}” from LeanCut? Its folder is moved aside, not deleted, and your original footage is never touched.`)) {
      await api(`/api/projects/${id}`, { method: "DELETE" }); await loadState();
    }
  } catch (err) { fail(err); }
};
$("#newProject").onsubmit = async (e) => {
  e.preventDefault();
  try { await post("/api/projects", { name: $("#newProjectName").value }); $("#newProjectName").value = ""; T = 0; sel = -1; await loadState(); showTab("editor"); } catch (err) { fail(err); }
};
$("#runQueue").onclick = () => {
  const projects = $$(".q").filter((c) => c.checked).map((c) => c.closest("tr").dataset.id);
  const steps = [$("#qAnalyze").checked && "analyze", $("#qExport").checked && "export"].filter(Boolean);
  showTab("editor");
  startJob("/api/queue", { projects, steps });
};

// ---------------------------------------------------------------- media library
function renderClips() {
  const clips = S.project.clips, mains = clips.filter((c) => !c.angle_of);
  $("#clips").innerHTML = clips.map((c, i) => {
    const others = mains.filter((m) => m.id !== c.id);
    const sync = clips.length > 1 ? `<span class="sync">Camera:
      <select data-act="sync" aria-label="Camera role for ${esc(c.name)}">
        <option value="">Main story</option>
        ${others.map((m) => `<option value="${m.id}" ${c.angle_of === m.id ? "selected" : ""}>Angle of ${esc(m.name)}</option>`).join("")}
      </select>${c.angle_of ? ` ${c.sync_offset >= 0 ? "+" : ""}${c.sync_offset.toFixed(2)} s` : ""}</span>` : "";
    return `<li data-id="${c.id}">
      <span class="name" title="${esc(c.path)}">${i + 1}. ${esc(c.name)}</span>
      <span class="btns">
        <button data-act="up" aria-label="Move up">↑</button><button data-act="down" aria-label="Move down">↓</button>
        <button data-act="del" aria-label="Remove clip">✕</button>
      </span>
      <span class="muted small">${c.recorded ? c.recorded.slice(5, 16).replace("T", " ") + " · " : ""}${fmtT(c.duration)} · ${fmtSize(c.size)} · ${c.width}x${c.height}${c.segments ? " · transcribed" : ""}</span>
      ${sync}
      <span class="extra"><button data-act="gyro" title="Use the gyro-stabilised file Gyroflow exported next to this clip">${c.original_path ? "Use original" : "Use Gyroflow export"}</button></span>
    </li>`;
  }).join("") || '<li class="muted">No clips yet</li>';
  $("#ovEmpty").classList.toggle("hidden", !!plan.parts.length);
  const rec = mains.map((c) => c.recorded).filter(Boolean);
  $("#sortClips").classList.toggle("hidden", rec.length < 2 || rec.every((r, i) => !i || rec[i - 1] <= r));
}
$("#sortClips").onclick = () => post("/api/clips/sort").then(loadState).then(() => toast("Clips are now in the order you recorded them.")).catch(fail);
$("#clips").onclick = async (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li || e.target.tagName === "SELECT" || e.target.tagName === "OPTION") return;
  const a = e.target.dataset.act, id = li.dataset.id;
  if (!a) return;
  try {
    if (a === "del") { if (!confirm("Remove this clip from the project? The file itself is not deleted.")) return; await api(`/api/clips/${id}`, { method: "DELETE" }); }
    else if (a === "gyro") await post(`/api/clips/${id}/stabilised`);
    else await post(`/api/clips/${id}/move`, { delta: a === "up" ? -1 : 1 });
    await loadState();
  } catch (err) { fail(err); }
};
$("#clips").onchange = async (e) => {
  if (e.target.dataset.act !== "sync") return;
  const id = e.target.closest("li").dataset.id;
  try {
    if (e.target.value) startJob(`/api/clips/${id}/sync`, { to: e.target.value });
    else { await api(`/api/clips/${id}/sync`, { method: "DELETE" }); await loadState(); }
  } catch (err) { fail(err); }
};
function upload(files) {
  if (!files.length) return;
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/clips/upload");
  xhr.upload.onprogress = (e) => { toast(`Uploading… ${Math.round((e.loaded / e.total) * 100)}%`); $("#bar").style.width = (e.loaded / e.total) * 100 + "%"; };
  xhr.onload = () => {
    if (xhr.status >= 400) fail(new Error(JSON.parse(xhr.responseText || "{}").detail || "Upload failed"));
    else toast(`Added ${files.length} clip(s).`);
    loadState();
  };
  xhr.onerror = () => fail(new Error("Upload failed"));
  xhr.send(fd);
}
const drop = $("#drop");
$("#files").onchange = (e) => upload(e.target.files);
drop.addEventListener("click", async (e) => {
  if (!desktop()) return;
  e.preventDefault();
  try {
    for (const path of (await desktop().pick_files()) || []) await post("/api/clips/path", { path });
    await loadState();
  } catch (err) { fail(err); }
});
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); };
$("#pathForm").onsubmit = async (e) => {
  e.preventDefault();
  try { await post("/api/clips/path", { path: $("#pathInput").value }); $("#pathInput").value = ""; await loadState(); } catch (err) { fail(err); }
};
window.addEventListener("pywebviewready", () => ($("#dropHint").textContent = "Picked files are used in place; camera chapters join automatically"));

// ---------------------------------------------------------------- programme plan & player
const pv = $("#pv"), pv2 = $("#pv2");
const introDur = () => +(plan.intro?.duration || 0);
const outroStart = () => plan.parts.length ? plan.parts[plan.parts.length - 1].rec_end : introDur();
let gen = 0, loading = false, selIds = new Set(), anchor = -1, snap = true;
async function loadPlan() {
  plan = await api("/api/plan");
  gfxVersion = Date.now(); $("#ovGfx").innerHTML = "";  // pages may have been rewritten (by you or an agent)
  const kept = plan.parts.filter((p) => !p.silent).reduce((a, p) => a + (p.rec_end - p.rec_start), 0);
  $("#summary").textContent = plan.parts.length ? `${plan.parts.length} parts · ${fmtT(plan.duration)} total${S.project.target_length ? " · target " + S.project.target_length : ""}` : "";
  $("#ovEmpty").classList.toggle("hidden", !!plan.parts.length);
  // selection is kept by item id, so it survives edits that renumber the parts
  const ids = new Set(plan.parts.flatMap((p) => p.ids));
  selIds = new Set([...selIds].filter((i) => ids.has(i)));
  sel = plan.parts.findIndex((p) => p.ids.some((i) => selIds.has(i)));
  T = Math.min(T, plan.duration);
  plan.parts.forEach((p) => loadMedia(p.clip));
  renderTimeline();
  renderInspector();
  renderGraphicsList();
  if (!playing) await showAt(T, false);
  return kept;
}
const selected = () => plan.parts.map((p, i) => i).filter((i) => plan.parts[i].ids.some((x) => selIds.has(x)));
function partAt(t) {
  if (t < introDur()) return "intro";
  if (t >= outroStart()) return plan.outro && t < plan.duration ? "outro" : -1;
  return plan.parts.findIndex((p) => t >= p.rec_start && t < p.rec_end);
}
const srcUrl = (p) => `/api/clips/${p.clip}/file${p.chapter != null ? "?chapter=" + p.chapter : ""}`;
// resolves on the event, or on error / after a timeout, so a broken file can never wedge the player
const once = (el, ev, ms = 8000) => new Promise((res) => {
  const done = () => { el.removeEventListener(ev, done); el.removeEventListener("error", done); clearTimeout(tm); res(); };
  const tm = setTimeout(done, ms);
  el.addEventListener(ev, done); el.addEventListener("error", done);
});

function gradeCss(g, effect) {
  if (!g) return "";
  const f = [`brightness(${(1 + 0.15 * (g.exposure || 0)).toFixed(3)})`, `contrast(${(g.contrast ?? 1).toFixed(3)})`,
    `saturate(${(g.saturation ?? 1).toFixed(3)})`];
  if (g.temperature > 0) f.push(`sepia(${Math.min(0.4, g.temperature * 0.3).toFixed(2)})`);
  if (g.temperature < 0) f.push(`hue-rotate(${(g.temperature * 14).toFixed(1)}deg)`);
  if (g.curves === "vintage") f.push("sepia(.25)");
  if (effect === "bw") f.push("grayscale(1)");
  if (effect === "dreamy") f.push("blur(.6px) brightness(1.06)");
  return f.join(" ");
}
function card(kind) {
  const c = kind === "intro" ? plan.intro : plan.outro;
  $("#ovCard").classList.toggle("hidden", !c);
  if (c) { $("#cardTitle").textContent = c.title; $("#cardSub").textContent = c.subtitle || ""; $("#cardSub").classList.toggle("hidden", !c.subtitle); }
}
// Every call gets a generation number: a slower, older call (still loading another file) never overrides a newer
// one, and nothing starts playing unless `playing` is still true when the file is ready (the old pause bug).
async function showAt(t, play) {
  const my = ++gen;
  T = Math.max(0, Math.min(t, plan.duration || 0));
  peek = false;
  if (mode === "rendered" && previewUrl) {
    $("#ovCard").classList.add("hidden"); hideCutaway();
    if (pv.dataset.url !== previewUrl) { loading = true; pv.src = previewUrl; pv.dataset.url = previewUrl; await once(pv, "loadedmetadata"); loading = false; if (my !== gen) return; }
    pv.style.filter = pv.style.transform = ""; $("#ovVignette").style.opacity = 0; $("#ovTexts").innerHTML = "";
    pv.currentTime = T; pv.playbackRate = 1; pv.muted = false;
    if (play && playing) pv.play().catch(() => {}); else pv.pause();
    return draw();
  }
  const k = partAt(T);
  if (k === "intro" || k === "outro") { pv.pause(); hideCutaway(); card(k); cur = k; return draw(); }
  $("#ovCard").classList.add("hidden");
  if (k < 0) { cur = -1; pv.pause(); hideCutaway(); return draw(); }
  const p = plan.parts[k];
  cur = k;
  const url = srcUrl(p);
  if (pv.dataset.url !== url) {
    loading = true; pv.pause(); pv.src = url; pv.dataset.url = url;
    await once(pv, "loadedmetadata");
    if (my !== gen) return;  // a newer seek took over; it owns `loading` now
    loading = false;
  }
  pv.currentTime = p.src_start + (T - p.rec_start) * p.speed;
  pv.playbackRate = Math.min(16, p.speed);
  pv.muted = !!p.silent;
  pv.style.filter = gradeCss(p.grade, p.effect);
  $("#ovVignette").style.opacity = Math.max(p.grade?.vignette || 0, p.effect === "vignette" ? 0.8 : 0);
  if (play && playing) await pv.play().catch(() => {});
  if (!playing) pv.pause();  // pressed pause while the file was loading
  draw();
}
function hideCutaway() { pv2.classList.add("hidden"); pv2.pause(); }
function cutaway(p, lt) {  // B-roll laid over the talking: a second video on top, muted, kept in sync
  const o = mode === "instant" && p ? (p.overlays || []).find((x) => lt >= x.t0 && lt < x.t1) : null;
  if (!o) return hideCutaway();
  const url = srcUrl(o), want = o.src_start + (lt - o.t0);
  pv2.classList.remove("hidden");
  pv2.style.filter = pv.style.filter;
  if (pv2.dataset.url !== url) { pv2.src = url; pv2.dataset.url = url; pv2.muted = true; }
  if (pv2.readyState >= 1 && Math.abs(pv2.currentTime - want) > 0.3) pv2.currentTime = want;
  if (playing && pv2.paused) pv2.play().catch(() => {});
  if (!playing && !pv2.paused) pv2.pause();
}
const textBox = (x, i, kind) => {
  const pos = x.x != null ? `left:${x.x * 100}%;top:${x.y * 100}%;transform:translate(-50%,-50%);` : "";
  const size = x.size ? `font-size:${(x.size * $("#monitor").clientHeight).toFixed(1)}px;` : "";
  return `<div class="tx ${x.style} ${kind}" data-k="${i}" data-kind="${kind}" style="${pos}${size}" title="Drag to move · double-click to edit">${x.style === "caption" ? `<span>${esc(x.text)}</span>` : esc(x.text)}</div>`;
};
// Graphics pages run in sandboxed iframes (no access to LeanCut), scaled to the monitor; each draw asks the page
// to draw its moment (window.film.frame(t)) through graphics-host.js, one request at a time per page.
function graphicsAt(p, lt) {
  const box = $("#ovGfx"), want = mode === "instant" && p ? (p.graphics || []).filter((g) => lt >= g.t0 && lt < g.t1) : [];
  [...box.children].forEach((f) => { if (!want.some((g) => g.id === f.dataset.id)) f.remove(); });
  const scale = $("#monitor").clientWidth / 1920;
  for (const g of want) {
    let f = box.querySelector(`[data-id="${g.id}"]`);
    if (!f) {
      f = document.createElement("iframe");
      f.dataset.id = g.id; f.setAttribute("sandbox", "allow-scripts"); f.setAttribute("aria-hidden", "true");
      f.src = `/api/graphics/page/${S.project.id}/${encodeURI(g.page.replace(/^graphics\//, ""))}?v=${gfxVersion}`;
      f.onload = () => { f.dataset.ready = "1"; f.dataset.t = ""; };
      box.appendChild(f);
    }
    f.style.transform = `scale(${scale})`;
    const gt = +(lt - g.t0 + (g.in || 0)).toFixed(3);  // a graphic runs on across cuts: `in` is where this part picks it up
    if (f.dataset.ready && f.dataset.t !== String(gt) && !(f._busy > performance.now() - 500)) {
      f._busy = performance.now(); f.dataset.t = String(gt);
      f.contentWindow.postMessage({ leancut: "frame", t: gt, id: g.id }, "*");
    }
  }
}
let gfxVersion = Date.now();
window.addEventListener("message", (e) => {
  if (e.data?.leancut !== "drawn" && e.data?.leancut !== "error") return;
  const f = [...$$("#ovGfx iframe")].find((x) => x.contentWindow === e.source);
  if (f) f._busy = 0;
  if (e.data.leancut === "error" || e.data.ok === false) appendLog(`Graphic ${f?.dataset.id || ""}: ${e.data.error || "page error"}`, true);
});
function overlays() {
  const box = $("#ovTexts");
  if (mode === "rendered" || typeof cur !== "number" || cur < 0) { if (!box.querySelector("[contenteditable]")) box.innerHTML = ""; hideCutaway(); graphicsAt(null, 0); return; }
  const p = plan.parts[cur], lt = T - p.rec_start, dur = p.rec_end - p.rec_start;
  graphicsAt(p, lt);
  const scale = p.effect === "zoom" ? 1.15 : p.effect === "push_in" ? 1 + 0.12 * Math.min(1, lt / dur) : 1;
  pv.style.transform = scale !== 1 ? `scale(${scale.toFixed(4)})` : "";
  cutaway(p, lt);
  if (box.querySelector("[contenteditable]") || dragging) return;  // don't redraw under the user's hands
  const html = p.texts.map((x, i) => (lt >= x.t0 && lt < x.t1 ? textBox(x, i, "text") : "")).filter(Boolean);
  html.push(...p.captions.map((x, i) => (lt >= x.t0 && lt < x.t1 ? textBox({ ...x, style: "caption" }, i, "cap") : "")).filter(Boolean));
  const key = cur + "|" + html.join("");
  if (box.dataset.key !== key) { box.innerHTML = html.join(""); box.dataset.key = key; }
  const next = plan.parts[cur + 1];  // approximate transitions as a short dip
  const into = p.transition && lt < 0.25 ? 1 - lt / 0.25 : 0;
  const out = next?.transition && dur - lt < 0.25 ? 1 - (dur - lt) / 0.25 : 0;
  $("#ovDim").style.opacity = Math.max(into, out) * 0.7;
}
function draw() {
  overlays();
  $("#tc").textContent = `${fmtT(T)} / ${fmtT(plan.duration)}`;
  $("#playhead").style.left = HEAD + T * pps() + "px";
  $("#playBtn").textContent = playing ? "❚❚" : "▶";
  $("#playBtn").setAttribute("aria-label", playing ? "Pause" : "Play");
  if (playing) follow();
}
let lastFrame = 0;
function loop(ts) {
  const dt = lastFrame ? (ts - lastFrame) / 1000 : 0;
  lastFrame = ts;
  if (playing && !peek && !loading) {
    if (mode === "rendered") { T = pv.currentTime; if (pv.ended) stop(); }
    else if (cur === "intro" || cur === "outro") {
      T += dt;
      if (cur === "intro" && T >= introDur()) showAt(T, true);
      if (cur === "outro" && T >= plan.duration) stop();
    } else if (typeof cur === "number" && cur >= 0) {
      const p = plan.parts[cur];
      if (!pv.seeking) T = p.rec_start + (pv.currentTime - p.src_start) / p.speed;
      if (pv.currentTime >= p.src_end - 0.04 || pv.ended) {
        T = p.rec_end + 0.001;
        if (T >= plan.duration - 0.01) stop(); else showAt(T, true);
      }
    } else if (T < plan.duration - 0.01) showAt(T, true); else stop();
    draw();
  }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
function play() { if (!plan.parts.length) return; playing = true; if (T >= plan.duration - 0.05) T = 0; showAt(T, true); }
function stop() { playing = false; pv.pause(); pv2.pause(); draw(); }
const toggle = () => (playing ? stop() : play());
$("#playBtn").onclick = toggle;
if (navigator.mediaSession) {  // keyboard media keys / headphones drive the same state
  navigator.mediaSession.setActionHandler("play", play);
  navigator.mediaSession.setActionHandler("pause", stop);
}
function syncMode() {
  $("#modeInstant").classList.toggle("on", mode === "instant");
  $("#modeRendered").classList.toggle("on", mode === "rendered");
  $("#modeRendered").disabled = !previewUrl;
}
$("#modeInstant").onclick = () => { mode = "instant"; syncMode(); pv.dataset.url = ""; showAt(T, playing); };
$("#modeRendered").onclick = () => { if (!previewUrl) return; mode = "rendered"; syncMode(); showAt(T, playing); };
$("#previewBtn").onclick = () => startJob("/api/preview");

// ---------------------------------------------------------------- text right on the video
// The item (transcript line / B-roll window) under the playhead, and how far into it we are, in source seconds.
function itemAtPlayhead() {
  const p = typeof cur === "number" ? plan.parts[cur] : null;
  if (!p || !p.ids.length) return null;
  const src = p.start + (T - p.rec_start) * p.speed;
  const its = p.ids.map((id) => ({ id, it: item(id) })).filter((x) => x.it);
  const hit = its.find((x) => src >= x.it.start && src < x.it.end) || its.reduce((a, b) => (Math.abs(b.it.start - src) < Math.abs(a.it.start - src) ? b : a));
  return { id: hit.id, offset: Math.max(0, src - hit.it.start), src, part: p };
}
$("#addTextBtn").onclick = async () => {
  const at = itemAtPlayhead();
  if (!at) return toast("Move the playhead onto a part of the video first.", true);
  stop();
  await act([{ type: "text", ids: [at.id], text: "Your text", value: "plain", offset: at.offset, duration: 4, x: 0.5, y: 0.45 }]);
  showAt(T + 0.01, false);
  const el = [...$$("#ovTexts .tx.text")].find((e) => e.textContent === "Your text");
  if (el) editOnMonitor(el);
};
let dragging = false;
function textRef(el) {
  const p = plan.parts[cur], k = +el.dataset.k;
  return el.dataset.kind === "cap" ? { cap: p.captions[k] } : { text: p.texts[k] };
}
function editOnMonitor(el) {
  const ref = textRef(el), target = el.querySelector("span") || el;
  stop();
  target.contentEditable = "true"; target.focus();
  document.getSelection().selectAllChildren(target);
  const finish = async (save) => {
    target.removeEventListener("blur", onBlur);
    target.contentEditable = "false";
    const v = target.textContent.trim();
    $("#ovTexts").dataset.key = "";
    if (!save) return draw();
    if (ref.cap) {
      if (v && v !== ref.cap.text) { const [, ci, k, i] = /^c(\d+)\.(s)(\d+)$/.exec(ref.cap.id); await patchItem(S.project.clips[+ci].id, "segments", +i, { text: v }); }
    } else if (ref.text.card) {
      const c = S.project[ref.text.card];
      if (v !== ref.text.text) await act([{ type: ref.text.card, text: ref.text.style === "caption" ? c.title : v, value: ref.text.style === "caption" ? v : c.subtitle, style: c.style, duration: c.duration, over: true }]);
    } else if (v !== ref.text.text) await act([{ type: "edit_text", ids: [ref.text.item], index: ref.text.index, text: v }]);
    draw();
  };
  const onBlur = () => finish(true);
  target.addEventListener("blur", onBlur);
  target.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); target.blur(); }
    if (e.key === "Escape") { target.removeEventListener("blur", onBlur); target.textContent = ref.cap?.text ?? ref.text.text; finish(false); }
  };
}
$("#ovTexts").addEventListener("dblclick", (e) => { const el = e.target.closest(".tx"); if (el) editOnMonitor(el); });
$("#ovTexts").addEventListener("pointerdown", (e) => {
  const el = e.target.closest(".tx.text");
  if (!el || el.querySelector("[contenteditable=true]") || el.isContentEditable) return;
  const ref = textRef(el).text;
  if (!ref || ref.card) return;  // intro/outro titles keep their layout
  e.preventDefault();
  const box = $("#monitor").getBoundingClientRect();
  let x = ref.x ?? 0.5, y = ref.y ?? 0.5, moved = false;
  const sx = e.clientX, sy = e.clientY;
  dragging = true; stop();
  const move = (ev) => {
    if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 3 && !moved) return;
    moved = true;
    x = Math.max(0.02, Math.min(0.98, (ev.clientX - box.left) / box.width));
    y = Math.max(0.03, Math.min(0.97, (ev.clientY - box.top) / box.height));
    if (snap && Math.abs(x - 0.5) < 0.02) x = 0.5;  // centre line
    Object.assign(el.style, { left: x * 100 + "%", top: y * 100 + "%", right: "auto", bottom: "auto", transform: "translate(-50%,-50%)" });
  };
  const up = async () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    dragging = false;
    if (moved) await act([{ type: "edit_text", ids: [ref.item], index: ref.index, x, y }]);
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
});
$("#monitor").addEventListener("wheel", (e) => {  // resize the text under the pointer
  const el = e.target.closest(".tx.text");
  if (!el) return;
  const ref = textRef(el).text;
  if (!ref || ref.card) return;
  e.preventDefault();
  const size = Math.max(0.02, Math.min(0.3, (ref.size || 0.05) * (e.deltaY < 0 ? 1.08 : 0.92)));
  ref.size = size; el.style.fontSize = size * $("#monitor").clientHeight + "px";
  clearTimeout(el._t); el._t = setTimeout(() => act([{ type: "edit_text", ids: [ref.item], index: ref.index, size }]), 400);
}, { passive: false });

// ---------------------------------------------------------------- timeline
const HEAD = 72;  // track-name column
const tl = $("#timeline");
const media = {};  // clip id -> {strip: {img, meta}, peaks}
function loadMedia(cid) {
  if (media[cid]) return;
  const m = (media[cid] = {});
  api(`/api/clips/${cid}/peaks`).then((r) => { m.peaks = r; paintLanes(); }).catch(() => {});
  api(`/api/clips/${cid}/strip`).then((meta) => {
    const img = new Image();
    img.onload = () => { m.strip = { img, meta }; paintLanes(); };
    img.src = meta.url;
  }).catch(() => {});
}
let zoomZ = 1;  // pixels per second = fit × zoomZ
const fitPps = () => Math.max(300, tl.clientWidth - HEAD - 20) / Math.max(plan.duration, 1);
const pps = () => fitPps() * zoomZ;
function setZoom(z, anchorT) {
  const t = anchorT ?? T, before = HEAD + t * pps() - tl.scrollLeft;
  zoomZ = Math.max(1, Math.min(400, z));
  $("#zoom").value = Math.round(Math.log2(zoomZ) * 12);
  renderTimeline();
  tl.scrollLeft = HEAD + t * pps() - before;
}
$("#zoom").oninput = (e) => setZoom(2 ** (+e.target.value / 12));
$("#zoomIn").onclick = () => setZoom(zoomZ * 1.5);
$("#zoomOut").onclick = () => setZoom(zoomZ / 1.5);
$("#zoomFit").onclick = () => setZoom(1);
$("#snapBtn").onclick = () => { snap = !snap; $("#snapBtn").classList.toggle("on", snap); };
function follow() {  // keep the playhead in view while playing
  const x = HEAD + T * pps();
  if (x > tl.scrollLeft + tl.clientWidth - 40 || x < tl.scrollLeft + HEAD) tl.scrollLeft = x - HEAD - 40;
}
const isBroll = (p) => p.ids.length && p.ids.every((i) => i.includes(".b"));
function label(p) {
  if (p.silent) return `⏩ ${p.speed.toFixed(0)}×`;
  const spoken = p.ids.map(item).find((x) => x?.text);
  const it = item(p.ids[0]);
  return spoken?.text || ("B-roll" + (it?.reason ? " · " + it.reason : ""));
}
const clipIdx = (cid) => S.project.clips.findIndex((c) => c.id === cid);
function renderTimeline() {
  const k = pps(), W = Math.max(plan.duration * k, 10);
  $$(".timeline .lane, #ruler").forEach((el) => (el.style.width = W + "px"));
  const step = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600].find((s) => s * k >= 70) || 1200;
  let ticks = "";
  for (let t = 0; t <= plan.duration; t += step) ticks += `<span style="left:${t * k}px">${fmtT(t).replace(/\.0$/, "")}</span>`;
  $("#ruler").innerHTML = ticks;
  const blk = (cls, x, w, inner, attrs = "") => `<div class="blk ${cls}" style="left:${x}px;width:${Math.max(3, w - 1)}px" ${attrs}>${inner}</div>`;
  const sels = new Set(selected());
  let v = "", a = "", b = "", t = "", vo = "", g = "";
  if (plan.intro) v += blk("card", 0, introDur() * k, "Intro · " + esc(plan.intro.title), 'data-card="intro"');
  plan.parts.forEach((p, i) => {
    const x = p.rec_start * k, w = (p.rec_end - p.rec_start) * k;
    const fx = [p.effect !== "none" && p.effect, p.speed !== 1 && !p.silent && `${p.speed}×`].filter(Boolean);
    const cls = `${isBroll(p) ? "broll" : ""} ${p.silent ? "ff" : ""} c${clipIdx(p.clip) % 4} ${sels.has(i) ? "sel" : ""}`;
    const handles = p.ids.length ? '<b class="h l" data-edge="l"></b><b class="h r" data-edge="r"></b>' : "";
    v += blk(cls, x, w, `${p.transition ? `<span class="tr" title="${esc(p.transition.type)}"></span>` : ""}<span class="lbl">${esc(label(p))}</span>${fx.map((f) => `<span class="fx">${esc(f)}</span>`).join("")}${handles}`,
      `data-i="${i}" title="${esc(label(p))}"`);
    a += blk(`${cls} wave`, x, w, "", `data-i="${i}"`);
    p.overlays.forEach((o) => (b += blk(`broll over ${selIds.has(o.id) ? "sel" : ""}`, (p.rec_start + o.t0) * k, (o.t1 - o.t0) * k, `<span class="lbl">${esc(item(o.id)?.reason || "B-roll")}</span>`, `data-o="${o.id}" data-i="${i}" title="B-roll over the talking · drag to move"`)));
    p.texts.forEach((x, j) => (t += blk(`text ${x.card ? "cardtx" : ""}`, (p.rec_start + x.t0) * k, (x.t1 - x.t0) * k, `${esc(x.text)}${x.card ? "" : '<b class="h r" data-edge="r"></b>'}`, `data-i="${i}" data-t="${j}" title="${esc(x.text)} · drag to move, double-click to edit"`)));
    (p.graphics || []).filter((x) => !x.in).forEach((x) => (g += blk("gfx", (p.rec_start + x.t0) * k, x.full * k, `<span class="lbl">✦ ${esc(x.template || x.page.replace(/^graphics\/|\.html$/g, ""))}</span><b class="h r" data-edge="r"></b>`, `data-i="${i}" data-g="${x.id}" title="${esc(x.page)} · drag to move, drag the end to change its length"`)));
    p.voiceovers.forEach((x) => (vo += blk("vo", (p.rec_start + x.t0) * k, Math.max(2, x.text.split(" ").length / 2.6) * k, "🎙 " + esc(x.text), `data-i="${i}"`)));
  });
  if (plan.outro) v += blk("card", outroStart() * k, (plan.duration - outroStart()) * k, "Outro · " + esc(plan.outro.title), 'data-card="outro"');
  $("#laneV").innerHTML = v; $("#laneA").innerHTML = a; $("#laneB").innerHTML = b; $("#laneT").innerHTML = t; $("#laneVO").innerHTML = vo; $("#laneG").innerHTML = g;
  $("#selInfo").textContent = sels.size > 1 ? `${sels.size} parts selected` : "";
  paintLanes();
  draw();
}
// Filmstrip and waveform: one canvas per lane, the size of the visible window, redrawn on scroll/zoom.
let paintReq = 0;
function paintLanes() { cancelAnimationFrame(paintReq); paintReq = requestAnimationFrame(paint); }
tl.addEventListener("scroll", paintLanes);
window.addEventListener("resize", () => renderTimeline());
function canvasFor(lane) {
  let cv = lane.parentElement.querySelector("canvas");
  if (!cv) { cv = document.createElement("canvas"); lane.parentElement.insertBefore(cv, lane); }
  const dpr = devicePixelRatio || 1, w = Math.max(10, tl.clientWidth - HEAD), h = lane.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); cv.style.width = w + "px"; cv.style.height = h + "px"; }
  cv.style.left = HEAD + tl.scrollLeft + "px";
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  return { g, w, h };
}
function paint() {
  const k = pps(), x0 = tl.scrollLeft;
  const V = canvasFor($("#laneV")), A = canvasFor($("#laneA"));
  for (const p of plan.parts) {
    const L = p.rec_start * k - x0, R = p.rec_end * k - x0;
    if (R < 0 || L > V.w) continue;
    const m = media[p.clip] || {}, srcAt = (x) => p.start + ((x + x0) / k - p.rec_start) * p.speed;
    if (m.strip) {  // thumbnails across the block, each showing the frame under it
      const { img, meta } = m.strip, th = V.h - 2, tw = th * 16 / 9;
      V.g.save(); V.g.beginPath(); V.g.rect(Math.max(0, L), 0, Math.min(V.w, R) - Math.max(0, L), V.h); V.g.clip();
      V.g.globalAlpha = p.silent ? 0.35 : 0.75;
      for (let x = Math.max(L, L + Math.floor((0 - L) / tw) * tw); x < Math.min(R, V.w); x += tw) {
        const f = Math.max(0, Math.min(meta.n - 1, Math.round(srcAt(x + tw / 2) / meta.step)));
        V.g.drawImage(img, (f % meta.cols) * meta.w, Math.floor(f / meta.cols) * meta.h, meta.w, meta.h, x, 1, tw, th);
      }
      V.g.restore();
    }
    if (m.peaks && !p.silent) {  // waveform, mirrored around the middle
      const pk = m.peaks.peaks, rate = m.peaks.rate, mid = A.h / 2;
      A.g.fillStyle = isBroll(p) ? "#d9a53a" : "#58c98b";
      for (let x = Math.max(0, Math.floor(L)); x < Math.min(R, A.w); x++) {
        const i0 = Math.floor(srcAt(x) * rate), i1 = Math.max(i0 + 1, Math.floor(srcAt(x + 1) * rate));
        let v = 0;
        for (let i = i0; i < i1 && i < pk.length; i++) v = Math.max(v, pk[i]);
        const hgt = Math.max(1, (v / 255) * (mid - 2));
        A.g.fillRect(x, mid - hgt, 1, hgt * 2);
      }
    }
  }
}
// time under the pointer, snapped to part edges / the playhead when it's close (8 px)
function timeAt(clientX, snapIt = snap) {
  const r = $("#laneV").getBoundingClientRect();
  let t = Math.max(0, Math.min(plan.duration, (clientX - r.left) / pps()));
  if (snapIt) {
    const edges = plan.parts.flatMap((p) => [p.rec_start, p.rec_end]);
    const near = edges.reduce((a, e) => (Math.abs(e - t) < Math.abs(a - t) ? e : a), Infinity);
    if (Math.abs(near - t) * pps() < 8) t = near;
  }
  return t;
}
function selectPart(i, e) {
  const p = plan.parts[i];
  if (!p) return;
  if (e?.shiftKey && anchor >= 0) {
    const [a, b] = [Math.min(anchor, i), Math.max(anchor, i)];
    plan.parts.slice(a, b + 1).forEach((q) => q.ids.forEach((x) => selIds.add(x)));
  } else if (e?.metaKey || e?.ctrlKey) {
    const on = p.ids.some((x) => selIds.has(x));
    p.ids.forEach((x) => (on ? selIds.delete(x) : selIds.add(x)));
    anchor = i;
  } else { selIds = new Set(p.ids); anchor = i; }
  sel = selected()[0] ?? -1;
  renderTimeline(); renderInspector(); openPane("insp");
}
function select(i, seek) {  // keyboard / deep links / suggestions
  if (!plan.parts[i]) return;
  selIds = new Set(plan.parts[i].ids); anchor = sel = i;
  renderTimeline(); renderInspector(); openPane("insp");
  if (seek) showAt(plan.parts[i].rec_start + 0.01, playing);
}
// drags on the timeline: scrub (ruler / empty space), trim part edges, move titles and cutaways
tl.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  const b = e.target.closest(".blk"), edge = e.target.dataset.edge;
  if (b?.dataset.card) { showAt(b.dataset.card === "intro" ? 0 : outroStart(), playing); openPane("look"); return; }
  if (b && edge && b.dataset.i != null && b.parentElement.id === "laneV") return trimDrag(e, +b.dataset.i, edge, b);
  if (b && b.dataset.t != null) return textDrag(e, +b.dataset.i, +b.dataset.t, b, edge);
  if (b && b.dataset.o) return overlayDrag(e, +b.dataset.i, b.dataset.o, b);
  if (b && b.dataset.g) return graphicDrag(e, +b.dataset.i, b.dataset.g, b, edge);
  if (b && b.dataset.i != null) { selectPart(+b.dataset.i, e); if (!e.shiftKey && !e.metaKey && !e.ctrlKey) showAt(timeAt(e.clientX, false), playing); return; }
  if (e.clientX - tl.getBoundingClientRect().left < HEAD) return;
  const was = playing; stop();  // scrub
  const move = (ev) => { T = timeAt(ev.clientX); showAt(T, false); };
  move(e);
  const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); if (was) play(); };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
});
function ghost(b, txt) { toast(txt); b.classList.add("drag"); }
function trimDrag(e, i, edge, b) {
  e.preventDefault(); e.stopPropagation(); stop();
  const p = plan.parts[i], id = edge === "l" ? p.ids[0] : p.ids[p.ids.length - 1], it = item(id), k = pps();
  const x0 = e.clientX, L = parseFloat(b.style.left), W = parseFloat(b.style.width);
  const words = (it.text ? wordsOf(it) : []).flatMap((w) => [w[1], w[2]]);
  let value = edge === "l" ? it.start : it.end;
  const move = (ev) => {
    const d = ((ev.clientX - x0) / k) * p.speed;
    value = edge === "l" ? Math.max(0, Math.min(it.end - 0.2, it.start + d)) : Math.max(it.start + 0.2, it.end + d);
    if (snap && words.length) {  // land between words, not in the middle of one
      const near = words.reduce((a, w) => (Math.abs(w - value) < Math.abs(a - value) ? w : a));
      if (Math.abs(near - value) / p.speed * k < 10) value = near;
    }
    const px = ((value - (edge === "l" ? it.start : it.end)) / p.speed) * k;
    if (edge === "l") { b.style.left = L + px + "px"; b.style.width = W - px + "px"; } else b.style.width = W + px + "px";
    ghost(b, `${edge === "l" ? "Start" : "End"} of ${id}: ${fmtT(value)} (${px >= 0 === (edge === "r") ? "longer" : "shorter"})`);
    showAt(p.rec_start + ((edge === "l" ? value - p.start : value - p.start) / p.speed), false);
  };
  const up = () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    act([{ type: "trim", ids: [id], [edge === "l" ? "start" : "end"]: +value.toFixed(2) }]);
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
}
function textDrag(e, i, j, b, edge) {
  e.preventDefault(); e.stopPropagation();
  const p = plan.parts[i], x = p.texts[j], k = pps(), x0 = e.clientX, L = parseFloat(b.style.left), W = parseFloat(b.style.width);
  if (x.card) { openPane("look"); return; }
  const base = item(x.item), off0 = base ? (base.texts || [])[x.index]?.offset || 0 : 0, dur0 = x.t1 - x.t0;
  let off = off0, dur = dur0, moved = false;
  const move = (ev) => {
    const d = (ev.clientX - x0) / k;
    moved = moved || Math.abs(ev.clientX - x0) > 3;
    if (edge) { dur = Math.max(0.5, dur0 + d); b.style.width = dur * k + "px"; ghost(b, `Title length ${dur.toFixed(1)} s`); }
    else { off = Math.max(0, off0 + d * p.speed); b.style.left = L + (off - off0) / p.speed * k + "px"; ghost(b, `Title starts ${off.toFixed(1)} s into ${x.item}`); }
  };
  const up = () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    if (moved) act([{ type: "edit_text", ids: [x.item], index: x.index, ...(edge ? { duration: +dur.toFixed(2) } : { offset: +off.toFixed(2) }) }]);
    else showAt(p.rec_start + x.t0 + 0.05, false);
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  b.ondblclick = () => { showAt(p.rec_start + x.t0 + 0.05, false); setTimeout(() => { const el = $("#ovTexts").querySelector(`.tx.text[data-k="${j}"]`); if (el) editOnMonitor(el); }, 150); };
}
function overlayDrag(e, i, oid, b) {
  e.preventDefault(); e.stopPropagation();
  const p = plan.parts[i], o = p.overlays.find((x) => x.id === oid), w = item(oid), k = pps(), x0 = e.clientX, L = parseFloat(b.style.left);
  const line = item(w.over), off0 = w.offset ?? 1;
  let off = off0, moved = false;
  selIds = new Set([oid]); renderInspector(); openPane("insp");
  const move = (ev) => {
    moved = moved || Math.abs(ev.clientX - x0) > 3;
    off = Math.max(0, Math.min(line.end - line.start - 0.5, off0 + (ev.clientX - x0) / k * p.speed));
    b.style.left = L + (off - off0) / p.speed * k + "px";
    ghost(b, `B-roll starts ${off.toFixed(1)} s into the line`);
  };
  const up = () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    if (moved) act([{ type: "broll_mode", ids: [oid], value: "cutaway", over: w.over, offset: +off.toFixed(2) }]);
    else showAt(p.rec_start + o.t0 + 0.05, false);
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
}
const tool = (name, args) => post("/api/agent/call", { name, arguments: args }).then((r) => {
  if (r.isError) throw new Error(r.content?.[0]?.text || name + " failed");
  return r;
});
function graphicDrag(e, i, gid, b, edge) {
  e.preventDefault(); e.stopPropagation();
  const p = plan.parts[i], x = p.graphics.find((q) => q.id === gid), k = pps(), x0 = e.clientX;
  const L = parseFloat(b.style.left), W = parseFloat(b.style.width);
  let at = p.rec_start + x.t0, dur = x.full, moved = false;
  const move = (ev) => {
    const d = (ev.clientX - x0) / k;
    moved = moved || Math.abs(ev.clientX - x0) > 3;
    if (edge) { dur = Math.max(0.3, x.full + d); b.style.width = dur * k + "px"; ghost(b, `Graphic length ${dur.toFixed(1)} s`); }
    else { at = Math.max(0, p.rec_start + x.t0 + d); b.style.left = L + d * k + "px"; ghost(b, `Graphic starts at ${fmtT(at)}`); }
  };
  const up = async () => {
    window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
    if (!moved) return showAt(p.rec_start + x.t0 + 0.05, false);
    try { await tool("update_graphic", edge ? { id: gid, duration: +dur.toFixed(2) } : { id: gid, at: +at.toFixed(2) }); await refresh(); } catch (err) { fail(err); renderTimeline(); }
  };
  window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
}
async function refresh() { S = await api("/api/state"); afterEdit({}); }
tl.addEventListener("wheel", (e) => {
  if (e.ctrlKey || e.metaKey) {  // pinch / ⌘-scroll zooms around the pointer
    e.preventDefault();
    setZoom(zoomZ * Math.exp(-e.deltaY * 0.01), timeAt(e.clientX, false));
  } else if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); tl.scrollLeft += e.deltaY; }
}, { passive: false });
function splitAtPlayhead() {
  const at = itemAtPlayhead();
  if (!at) return toast("Put the playhead on a part to split it.", true);
  act([{ type: "split", ids: [at.id], start: +at.src.toFixed(2) }]);
}
function cutSelection() {
  const ids = selected().flatMap((i) => plan.parts[i].ids);
  const overs = [...selIds].filter((x) => !ids.includes(x));
  if (ids.length || overs.length) act([{ type: "cut", ids: [...ids, ...overs] }]);
}
$("#splitBtn").onclick = splitAtPlayhead;
$("#cutBtn").onclick = cutSelection;
document.addEventListener("keydown", (e) => {
  if (e.target.matches?.("input, textarea, select, [contenteditable=true]") || $("#editor").classList.contains("hidden")) return;
  const mod = e.metaKey || e.ctrlKey, key = e.key.toLowerCase();
  // a focused button would also "click" on Space, toggling twice: take focus off it first
  if (e.code === "Space" || key === "k") { e.preventDefault(); if (e.target.closest("button")) e.target.blur(); key === "k" ? stop() : toggle(); return; }
  if ((e.key === "Delete" || e.key === "Backspace") && wordSelection()) { e.preventDefault(); return cutWords(); }
  if (mod && key === "z") { e.preventDefault(); (e.shiftKey ? $("#redoBtn") : $("#undoBtn")).click(); }
  else if (mod && key === "a") { e.preventDefault(); selIds = new Set(plan.parts.flatMap((p) => p.ids)); sel = 0; renderTimeline(); renderInspector(); }
  else if (mod) return;
  else if (e.key === "ArrowRight") { e.preventDefault(); select(Math.min(plan.parts.length - 1, sel + 1), true); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); select(Math.max(0, sel - 1), true); }
  else if (e.key === "," || e.key === ".") { stop(); showAt(T + (e.key === "," ? -1 : 1) / 30, false); }
  else if (key === "j") showAt(Math.max(0, T - 5), playing);
  else if (key === "l") play();
  else if (key === "s" || key === "b") splitAtPlayhead();
  else if (key === "t") $("#addTextBtn").click();
  else if (e.key === "+" || e.key === "=") setZoom(zoomZ * 1.5);
  else if (e.key === "-") setZoom(zoomZ / 1.5);
  else if (e.key === "Home") showAt(0, playing);
  else if (e.key === "End") showAt(plan.duration - 0.05, false);
  else if (e.key === "Escape") { selIds = new Set(); sel = -1; renderTimeline(); renderInspector(); }
  else if ((e.key === "Delete" || e.key === "Backspace") && selIds.size) { e.preventDefault(); cutSelection(); }
});
$("#undoBtn").onclick = () => post("/api/undo").then(afterEdit).catch(fail);
$("#redoBtn").onclick = () => post("/api/redo").then(afterEdit).catch(fail);

// ---------------------------------------------------------------- inspector
function item(id) {
  const m = /^c(\d+)\.([sb])(\d+)$/.exec(id || "");
  if (!m) return null;
  const c = S.project.clips[+m[1]];
  return c?.[m[2] === "s" ? "segments" : "broll"]?.[+m[3]] || null;
}
const opts = (obj, v) => Object.entries(obj).map(([k, n]) => `<option value="${k}" ${k === v ? "selected" : ""}>${esc(typeof n === "string" ? n : k.replace("_", " "))}</option>`).join("");
function renderInspector() {
  const box = $("#pane-insp");
  const many = selected();
  const over = [...selIds].find((x) => item(x)?.over && !many.length);
  if (over) return renderCutaway(box, over);
  if (many.length > 1) return renderMulti(box, many);
  const p = plan.parts[sel];
  if (!p) { box.innerHTML = '<p class="muted">Select a part on the timeline (⇧-click or ⌘-click to select several). Space plays, S splits at the playhead, T adds text on the video.</p>'; return; }
  const first = item(p.ids[0]), last = item(p.ids[p.ids.length - 1]);
  const texts = p.ids.flatMap((id) => (item(id)?.texts || []).map((t) => ({ ...t, id })));
  const vos = p.ids.flatMap((id) => (item(id)?.voiceovers || []).map((t) => ({ ...t, id })));
  const sugs = (S.project.suggestions || []).filter((x) => x.status === "open" && (x.ids || []).some((i) => p.ids.includes(i)));
  const trType = first?.transition?.type || "cut";
  box.innerHTML = `<div class="insp">
    <fieldset><legend>Part ${sel + 1} of ${plan.parts.length}</legend>
      <p class="small">${p.ids.map((i) => `<span class="chip">${i}</span>`).join(" ")}</p>
      <p class="small muted">${fmtT(p.start)}–${fmtT(p.end)} of ${esc(S.project.clips.find((c) => c.id === p.clip)?.name)} · ${fmtT(p.rec_end - p.rec_start)} in the video</p>
      <p class="small">${p.silent ? "Fast-forward ride-through" : esc(isBroll(p) ? "Scenic B-roll" : p.ids.map((i) => item(i)?.text).join(" "))}</p>
      <div class="row wrap"><button id="iPlay">▶ Play from here</button>${p.ids.length ? '<button id="iCut">Cut this part</button><button id="iSplit" title="S">Split at playhead</button>' : ""}${isBroll(p) ? '<button id="iOver" title="Show this B-roll over the nearest line you say, instead of on its own">Lay over the talking</button>' : ""}</div>
      ${first && last ? `<label>Trim start (s) <input type="number" id="iStart" step="0.1" value="${first.start}"></label>
      <label>Trim end (s) <input type="number" id="iEnd" step="0.1" value="${last.end}"></label>` : ""}
    </fieldset>
    ${p.ids.length ? `<fieldset><legend>Look</legend>
      <label>Effect <select id="iEffect">${opts(S.effects, first?.effect || "none")}</select></label>
      <label>Transition into this part <select id="iTrans">${opts(S.transitions, trType)}</select></label>
      <label>Transition length (s) <input type="number" id="iTransDur" min="0.1" max="2" step="0.1" value="${first?.transition?.dur || 0.6}"></label>
      <label>Color for this part <select id="iGrade"><option value="">Same as the video</option>${opts(S.presets, "")}</select></label>
    </fieldset>
    <fieldset><legend>Titles</legend>
      <ul class="items">${texts.map((t) => `<li><span>${esc(t.text)} <span class="muted">(${t.style}, ${t.duration}s)</span></span><button data-rmtext="${t.id}">✕</button></li>`).join("") || '<li class="muted">No titles</li>'}</ul>
      <label>Text <input id="iText" placeholder="e.g. Day 2 · Nagarkot"></label>
      <div class="row"><select id="iStyle">${S.text_styles.map((s) => `<option value="${s}">${s.replace("_", " ")}</option>`).join("")}</select>
        <input type="number" id="iTextAt" step="0.5" value="0" title="Seconds into the part" aria-label="Offset (s)">
        <input type="number" id="iTextDur" step="0.5" value="4" title="Duration (s)" aria-label="Duration (s)"></div>
      <button id="iAddText">Add title</button>
    </fieldset>
    <fieldset><legend>Voice-over (your voice)</legend>
      <ul class="items">${vos.map((t) => `<li><span>🎙 ${esc(t.text)}</span><button data-rmvo="${t.id}">✕</button></li>`).join("") || '<li class="muted">No voice-over</li>'}</ul>
      <label>Line <textarea id="iVo" rows="2" placeholder="e.g. Forty minutes later, the fog finally lifted."></textarea></label>
      <div class="row"><input type="number" id="iVoAt" step="0.5" value="0" aria-label="Offset (s)" title="Seconds into the part"><button id="iHear">Hear it</button><button id="iAddVo">Add voice-over</button></div>
    </fieldset>` : ""}
    <fieldset><legend>Suggestions for this part</legend>
      ${sugs.map((x) => sugCard(x)).join("") || '<p class="muted small">None. Ask the assistant for ideas.</p>'}
    </fieldset></div>`;
  const id0 = p.ids[0];
  $("#iPlay").onclick = () => { playing = true; showAt(p.rec_start + 0.01, true); };
  $("#iCut") && ($("#iCut").onclick = () => act([{ type: "cut", ids: p.ids }]));
  $("#iSplit") && ($("#iSplit").onclick = splitAtPlayhead);
  $("#iOver") && ($("#iOver").onclick = () => act([{ type: "broll_mode", ids: p.ids, value: "cutaway" }]));
  $("#iStart") && ($("#iStart").onchange = (e) => act([{ type: "trim", ids: [p.ids[0]], start: +e.target.value }]));
  $("#iEnd") && ($("#iEnd").onchange = (e) => act([{ type: "trim", ids: [p.ids[p.ids.length - 1]], end: +e.target.value }]));
  if (!p.ids.length) return;
  $("#iEffect").onchange = (e) => act([{ type: "effect", ids: p.ids, value: e.target.value }]);
  const tr = () => act([{ type: "transition", ids: [id0], value: $("#iTrans").value, duration: +$("#iTransDur").value }]);
  $("#iTrans").onchange = tr; $("#iTransDur").onchange = tr;
  $("#iGrade").onchange = (e) => act([{ type: "grade", ids: p.ids, value: e.target.value || "none" }]);
  $("#iAddText").onclick = () => $("#iText").value.trim() && act([{ type: "text", ids: [id0], text: $("#iText").value, value: $("#iStyle").value, offset: +$("#iTextAt").value, duration: +$("#iTextDur").value }]);
  $("#iAddVo").onclick = () => $("#iVo").value.trim() && act([{ type: "voiceover", ids: [id0], text: $("#iVo").value, offset: +$("#iVoAt").value }]);
  $("#iHear").onclick = () => $("#iVo").value.trim() && startJob("/api/voice/test", { text: $("#iVo").value });
  box.querySelectorAll("[data-rmtext]").forEach((b) => (b.onclick = () => act([{ type: "remove_text", ids: [b.dataset.rmtext] }])));
  box.querySelectorAll("[data-rmvo]").forEach((b) => (b.onclick = () => act([{ type: "remove_voiceover", ids: [b.dataset.rmvo] }])));
  bindSugs(box);
}

function renderMulti(box, many) {  // one edit for every selected part
  const ids = many.flatMap((i) => plan.parts[i].ids), len = many.reduce((a, i) => a + plan.parts[i].rec_end - plan.parts[i].rec_start, 0);
  box.innerHTML = `<div class="insp">
    <fieldset><legend>${many.length} parts selected · ${fmtT(len)}</legend>
      <p class="small muted">Changes here apply to all of them. ⇧-click extends the selection, ⌘-click adds or removes one, Esc clears.</p>
      <div class="row wrap"><button id="mCut">Cut all</button><button id="mStab">Stabilise all</button><button id="mClear">Clear selection</button></div>
    </fieldset>
    <fieldset><legend>Look</legend>
      <label>Effect <select id="mEffect"><option value="">—</option>${opts(S.effects, "")}</select></label>
      <label>Transition into each <select id="mTrans"><option value="">—</option>${opts(S.transitions, "")}</select></label>
      <label>Color <select id="mGrade"><option value="">—</option><option value="none">Same as the video</option>${opts(S.presets, "")}</select></label>
    </fieldset>
    <fieldset><legend>Title on each part</legend>
      <label>Text <input id="mText" placeholder="e.g. Day 2"></label>
      <div class="row"><select id="mStyle">${S.text_styles.map((s) => `<option value="${s}">${s.replace("_", " ")}</option>`).join("")}</select><button id="mAddText">Add</button><button id="mRmText">Remove titles</button></div>
    </fieldset></div>`;
  const firsts = many.map((i) => plan.parts[i].ids[0]).filter(Boolean);
  $("#mCut").onclick = () => act([{ type: "cut", ids }]);
  $("#mStab").onclick = () => act([{ type: "stabilise", ids, value: "on" }]);
  $("#mClear").onclick = () => { selIds = new Set(); sel = -1; renderTimeline(); renderInspector(); };
  $("#mEffect").onchange = (e) => e.target.value && act([{ type: "effect", ids, value: e.target.value }]);
  $("#mTrans").onchange = (e) => e.target.value && act([{ type: "transition", ids: firsts, value: e.target.value }]);
  $("#mGrade").onchange = (e) => e.target.value && act([{ type: "grade", ids, value: e.target.value }]);
  $("#mAddText").onclick = () => $("#mText").value.trim() && act(firsts.map((id) => ({ type: "text", ids: [id], text: $("#mText").value, value: $("#mStyle").value, duration: 4 })));
  $("#mRmText").onclick = () => act([{ type: "remove_text", ids }]);
}
function renderCutaway(box, oid) {
  const w = item(oid), line = item(w.over);
  box.innerHTML = `<div class="insp"><fieldset><legend>B-roll over the talking · ${oid}</legend>
    <p class="small">${esc(w.reason || "Scenic B-roll")}</p>
    <p class="small muted">Laid over “${esc((line?.text || "").slice(0, 80))}” (${w.over}), ${(w.offset ?? 1).toFixed(1)} s in. The talking keeps playing underneath. Drag it on the B-roll track to move it.</p>
    <div class="row wrap"><button id="oInsert">Make it its own shot</button><button id="oCut">Remove</button></div>
  </fieldset></div>`;
  $("#oInsert").onclick = () => act([{ type: "broll_mode", ids: [oid], value: "insert" }]);
  $("#oCut").onclick = () => { selIds = new Set(); act([{ type: "cut", ids: [oid] }]); };
}

// ---------------------------------------------------------------- graphics pane: templates and the video's graphics
let templates = [];
async function loadTemplates() {
  const r = await api("/api/graphics/templates").catch(() => ({ templates: [] }));
  templates = r.templates;
  $("#gfxTemplates").innerHTML = templates.map((t, k) => `<button class="gfxcard" data-k="${k}"><b>${esc(t.name.replace(/_/g, " "))}</b><span>${esc(t.description || "")}</span></button>`).join("") ||
    '<p class="muted small">Templates need the graphics module and a Chrome-based browser.</p>';
}
$("#gfxTemplates").onclick = (e) => {
  const b = e.target.closest(".gfxcard");
  if (!b) return;
  const t = templates[+b.dataset.k], data = /speed|route|map/.test(t.name) || t.data;
  $("#gfxForm").classList.remove("hidden");
  $("#gfxForm").innerHTML = `<b>${esc(t.name.replace(/_/g, " "))}</b>
    ${Object.entries(t.fields || {}).map(([k, f]) => `<label>${esc(f.label || k)} <input data-f="${esc(k)}" value="${esc(f.default ?? "")}"></label>`).join("")}
    ${data ? `<label>GPS from clip <select id="gfxClip">${S.project.clips.map((c, i) => `<option value="${i}">${i + 1}. ${esc(c.name)}</option>`).join("")}</select></label>` : ""}
    <label>Length (s) <input type="number" id="gfxDur" min="0.5" step="0.5" value="${t.duration || 4}"></label>
    <div class="row"><button class="primary" id="gfxAdd">Add at the playhead (${fmtT(T)})</button><button id="gfxCancel">Cancel</button></div>`;
  $("#gfxCancel").onclick = () => $("#gfxForm").classList.add("hidden");
  $("#gfxAdd").onclick = async () => {
    const fields = Object.fromEntries($$("#gfxForm [data-f]").map((i) => [i.dataset.f, i.value]));
    try {
      await afterEdit(await post("/api/graphics/add", { template: t.name, fields, at: +T.toFixed(2), duration: +$("#gfxDur").value, ...(data ? { clip: +$("#gfxClip").value } : {}) }));
      $("#gfxForm").classList.add("hidden");
      showAt(T + 0.05, false);
    } catch (err) { fail(err); }
  };
};
function renderGraphicsList() {
  const rows = plan.parts.flatMap((p) => (p.graphics || []).filter((g) => !g.in).map((g) => ({ ...g, at: p.rec_start + g.t0 })));
  $("#gfxList").innerHTML = rows.map((g) => `<li><span>✦ ${esc(g.template || g.page)} <span class="muted">${fmtT(g.at)} · ${(+g.full).toFixed(1)} s · ${esc(g.page)}</span></span>
    <span><button data-gjump="${g.at}">Show</button> <button data-grm="${g.id}">✕</button></span></li>`).join("") || '<li class="muted">No graphics yet.</li>';
}
$("#gfxList").onclick = (e) => {
  if (e.target.dataset.gjump) showAt(+e.target.dataset.gjump + 0.05, false);
  if (e.target.dataset.grm) act([{ type: "remove_graphic", value: e.target.dataset.grm }]);
};

// ---------------------------------------------------------------- look & titles
function renderLook() {
  const pr = S.project, g = pr.grade || {}, m = g.manual || {};
  $("#cutStyle").value = pr.cut_style?.type || "cut";
  $("#cutDur").value = pr.cut_style?.dur || 0.4;
  $("#autoGrade").checked = !!g.auto;
  $("#preset").value = g.preset || "none";
  $$("[data-g]").forEach((el) => (el.value = m[el.dataset.g] ?? { contrast: 1, saturation: 1 }[el.dataset.g] ?? 0));
  $("#lutPath").value = m.lut || "";
  $("#introTitle").value = pr.intro?.title || ""; $("#introSub").value = pr.intro?.subtitle || "";
  $("#introStyle").value = pr.intro?.style || "bold";
  $("#introOver").checked = !!pr.intro?.over;
  $("#outroTitle").value = pr.outro?.title || ""; $("#outroSub").value = pr.outro?.subtitle || "";
  $("#captions").checked = !!pr.captions; $("#privacy").checked = !!pr.privacy_blur;
  $("#brollShare").value = pr.broll_share ?? S.settings.broll_share; $("#musicVol").value = pr.music_volume ?? S.settings.music_volume;
}
const cutStyle = () => act([{ type: "cut_style", value: $("#cutStyle").value, duration: +$("#cutDur").value }]);
$("#cutStyle").onchange = cutStyle; $("#cutDur").onchange = cutStyle;
$("#autoGrade").onchange = (e) => act([{ type: "auto_grade", value: e.target.checked ? "on" : "off" }]);
$("#preset").onchange = (e) => act([{ type: "grade", value: e.target.value }]);
$$("[data-g]").forEach((el) => (el.onchange = () => act([{ type: "grade", params: { [el.dataset.g]: +el.value } }])));
$("#resetGrade").onclick = () => act([{ type: "grade", params: { exposure: 0, contrast: 1, saturation: 1, temperature: 0, vignette: 0 } }, { type: "lut", value: "" }]);
$("#lutPath").onchange = (e) => act([{ type: "lut", value: e.target.value.trim() }]);
$("#pickLut").onclick = async () => { if (!desktop()) return $("#lutPath").focus(); const f = await desktop().pick_lut(); if (f) act([{ type: "lut", value: f }]); };
$("#saveCards").onclick = () => act([
  { type: "intro", text: $("#introTitle").value, value: $("#introSub").value, style: $("#introStyle").value, duration: 4, over: $("#introOver").checked },
  { type: "outro", text: $("#outroTitle").value, value: $("#outroSub").value, style: $("#introStyle").value, duration: 4, over: $("#introOver").checked }]);
$("#captions").onchange = (e) => act([{ type: "captions", value: e.target.checked ? "on" : "off" }]);
$("#privacy").onchange = (e) => act([{ type: "privacy_blur", value: e.target.checked ? "on" : "off" }]);
$("#brollShare").onchange = (e) => act([{ type: "broll_share", amount: +e.target.value }]);
$("#musicVol").onchange = (e) => act([{ type: "music_volume", amount: +e.target.value }]);

// ---------------------------------------------------------------- assistant: chat & suggestions
const agentName = (id) => agents.find((a) => a.id === id)?.label.replace(/ \(.*\)$/, "") || "";
function renderChat() {
  const msgs = S.project.chat || [];
  $("#chatLog").innerHTML = (msgs.length ? "" : '<p class="muted small">Tell me what to change, in your own words. Every change can be undone. Pick “Agent with tools” or a coding agent above for bigger jobs, like animated graphics.</p>') +
    msgs.map((m) => `<div class="msg ${m.role}">${m.role === "assistant" && m.agent ? `<span class="who">${esc(agentName(m.agent))}</span>` : ""}${esc(m.text)}${m.done?.length ? `<ul>${m.done.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}${m.steps?.length ? `<details><summary>${m.steps.filter((x) => x.kind === "tool").length} tool calls</summary><ul>${m.steps.map((x) => `<li>${x.kind === "tool" ? "🔧 " : x.kind === "say" ? "💬 " : "↳ "}${esc(x.text)}</li>`).join("")}</ul></details>` : ""}${m.errors?.length ? `<div class="errs">${m.errors.map(esc).join("<br>")}</div>` : ""}</div>`).join("");
  $("#chatLog").scrollTop = 1e9;
}
let agents = [], agentKind = "assistant";
try { agentKind = localStorage.getItem("leancut.agent") || "assistant"; } catch { /* storage blocked */ }
async function loadAgents() {
  const r = await api("/api/agent/agents").catch(() => null);
  if (!r) return;
  agents = r.agents;
  if (!agents.some((a) => a.id === agentKind)) agentKind = "assistant";
  $("#agentPick").innerHTML = agents.map((a) => `<option value="${a.id}" ${a.ready ? "" : "disabled"}>${esc(a.label)}${a.ready ? "" : " · not installed"}</option>`).join("");
  $("#agentPick").value = agentKind;
  $("#agentList").innerHTML = agents.filter((a) => !["assistant", "local"].includes(a.id)).map((a) => `<li><b style="min-width:220px">${esc(a.label)}</b>${a.ready ? '<span class="ok">✓ found</span>' : '<span class="muted">not installed</span>'}</li>`).join("");
  $("#mcpCmd").textContent = r.mcp_command.map((x) => (/\s/.test(x) ? `"${x}"` : x)).join(" ");
  $("#connectList").innerHTML = Object.entries(r.connect || {}).map(([k, c]) => `<details><summary>Connect ${esc(k)}</summary><p class="small">${esc(c.how || "")}</p>${c.cli ? `<pre>${esc(c.cli)}</pre>` : ""}${c.snippet ? `<p class="small muted">${esc(c.path || "")}</p><pre>${esc(c.snippet)}</pre>` : ""}</details>`).join("");
  renderChat();
}
$("#agentPick").onchange = (e) => { agentKind = e.target.value; try { localStorage.setItem("leancut.agent", agentKind); } catch { /* ignore */ } };
$("#agentHelp").onclick = () => { showTab("settings"); $("#agentsPanel").scrollIntoView(); };
$("#chips").onclick = (e) => { if (e.target.tagName === "BUTTON") { $("#chatInput").value = e.target.textContent; $("#chatInput").focus(); } };
$("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chatForm").requestSubmit(); } });
$("#chatForm").onsubmit = async (e) => {
  e.preventDefault();
  const message = $("#chatInput").value.trim();
  if (!message) return;
  $("#chatInput").value = "";
  $("#chatLog").insertAdjacentHTML("beforeend", `<div class="msg user">${esc(message)}</div><div class="msg assistant muted working" id="thinking">${agentKind === "assistant" ? "Thinking…" : esc(agentName(agentKind)) + " is working…<div class=\"live\"></div>"}</div>`);
  $("#chatLog").scrollTop = 1e9;
  if (agentKind !== "assistant") {  // a job: steps stream into the bubble, the chat reloads when it's done
    try { logCount = 0; $("#log").innerHTML = ""; await post("/api/agent/run", { message, agent: agentKind }); wasRunning = true; poll(); }
    catch (err) { $("#thinking")?.remove(); $("#chatLog").insertAdjacentHTML("beforeend", `<div class="msg assistant errs">${esc(err.message)}</div>`); }
    return;
  }
  $("#chatSend").disabled = true;
  try { await afterEdit(await post("/api/chat", { message })); }
  catch (err) { $("#thinking")?.remove(); $("#chatLog").insertAdjacentHTML("beforeend", `<div class="msg assistant errs">${esc(err.message)}</div>`); }
  finally { $("#chatSend").disabled = false; }
};
function sugCard(x) {
  return `<div class="sug ${x.status !== "open" ? "done" : ""}" data-sid="${x.sid}">
    <b>${esc(x.title)} ${x.source === "ai" ? '<span class="src-ai">AI</span>' : ""}</b>
    <div class="why">${esc(x.why)}</div>
    ${(x.ids || []).length ? `<div class="ids">${x.ids.map((i) => `<span class="chip" data-goto="${i}">${i}</span>`).join("")}</div>` : ""}
    ${x.status === "open" ? '<div class="row"><button class="primary" data-sv="apply">Apply</button><button data-sv="dismiss">Dismiss</button></div>' : `<span class="muted small">${x.status}</span>`}
  </div>`;
}
function bindSugs(root) {
  root.querySelectorAll("[data-sv]").forEach((b) => (b.onclick = () => {
    const sid = b.closest(".sug").dataset.sid;
    post(`/api/suggestions/${sid}/${b.dataset.sv}`).then(afterEdit).catch(fail);
  }));
  root.querySelectorAll("[data-goto]").forEach((c) => (c.onclick = () => {
    const i = plan.parts.findIndex((p) => p.ids.includes(c.dataset.goto));
    if (i >= 0) select(i, true); else toast(`${c.dataset.goto} isn't in the cut right now.`);
  }));
}
function renderSuggestions() {
  const all = S.project.suggestions || [], open = all.filter((x) => x.status === "open");
  $("#suggCount").textContent = open.length;
  $("#suggList").innerHTML = [...open, ...all.filter((x) => x.status !== "open")].map(sugCard).join("") || '<p class="muted small">Suggestions appear after the auto-edit.</p>';
  bindSugs($("#suggList"));
}
$("#suggestBtn").onclick = async () => {
  $("#suggestBtn").disabled = true; $("#suggestBtn").textContent = "Thinking…";
  try { await afterEdit(await post("/api/suggest")); } catch (e) { fail(e); }
  finally { $("#suggestBtn").disabled = false; $("#suggestBtn").textContent = "Suggest again"; }
};
$("#applyAllBtn").onclick = async () => {
  for (const x of (S.project.suggestions || []).filter((s) => s.status === "open")) {
    try { await afterEdit(await post(`/api/suggestions/${x.sid}/apply`)); } catch (e) { appendLog(`Skipped “${x.title}”: ${e.message}`, true); }
  }
};

// ---------------------------------------------------------------- transcript (source view)
const items = (c) => [
  ...(c.segments || []).map((x, i) => ({ ...x, kind: "segments", i })),
  ...(c.broll || []).map((x, i) => ({ ...x, kind: "broll", i })),
].sort((a, b) => a.start - b.start);
// Whisper's word times when stored; older transcripts spread the words evenly over the line (close enough to cut)
const wordsOf = (s) => {
  if (s.words?.length) return s.words;
  const ws = s.text.split(/\s+/).filter(Boolean), step = (s.end - s.start) / Math.max(1, ws.length);
  return ws.map((w, k) => [w, +(s.start + k * step).toFixed(2), +(s.start + (k + 1) * step).toFixed(2)]);
};
function renderTranscript() {
  const tracks = [], text = [];
  S.project.clips.forEach((c, ci) => {
    const all = items(c);
    const lab = c.angle_of ? ` <span class="muted">· second angle</span>` : "";
    tracks.push(`<div class="strack"><div class="lbl">${ci + 1}. ${esc(c.name)}${lab}</div><div class="slane" data-id="${c.id}">${all.map((s) =>
      `<i class="${s.keep ? "k" : ""} ${s.kind === "broll" ? "b" : ""}" data-kind="${s.kind}" data-i="${s.i}" title="${esc(s.text || s.reason || "")}"
        style="left:${(s.start / c.duration) * 100}%;width:${Math.max(0.15, ((s.end - s.start) / c.duration) * 100)}%">${s.keep ? '<b class="h l"></b><b class="h r"></b>' : ""}</i>`).join("")}</div></div>`);
    if (!all.length) return;
    text.push(`<h3>${ci + 1}. ${esc(c.name)}</h3>`);
    all.forEach((s) => {
      const d = s.end - s.start;
      const lbl = s.kind === "broll" ? `Scenic B-roll · ${d.toFixed(1)} s${s.over ? " · over " + s.over : ""}`
        : wordsOf(s).map((w) => `<span class="w" data-a="${w[1]}" data-b="${w[2]}">${esc(w[0])}</span>`).join(" ");
      const why = s.kind === "broll" ? s.reason : (s.score != null ? `★${s.score} ${s.reason || ""}` : "");
      const unsure = s.conf != null && s.conf < 0.55 ? ` <span class="unsure" title="The AI is only ${Math.round(s.conf * 100)}% sure">?</span>` : "";
      text.push(`<div class="seg ${s.keep ? "k" : ""} ${s.kind === "broll" ? "broll" : ""}" data-id="${c.id}" data-kind="${s.kind}" data-i="${s.i}">
        <input type="checkbox" ${s.keep ? "checked" : ""} aria-label="Keep">
        <span class="t">${fmtT(s.start)}</span>
        <span class="txt">${lbl}${why ? `<span class="why">${esc(why)}</span>` : ""}${unsure}</span></div>`);
    });
  });
  $("#tracks").innerHTML = tracks.join("");
  $("#transcript").innerHTML = text.join("") || '<p class="muted">Run “Auto-edit with AI” to transcribe and get the AI’s cut.</p>';
}
async function patchItem(cid, kind, i, body) {
  const c = S.project.clips.find((x) => x.id === cid);
  Object.assign(c[kind][i], await api(`/api/clips/${cid}/${kind}/${i}`, { method: "PATCH", body: JSON.stringify(body) }));
  $("#undoBtn").disabled = false;
  renderTranscript();
  await loadPlan();
}
function peekSource(cid, t) {
  stop(); peek = true; mode = "instant"; syncMode();
  const c = S.project.clips.find((x) => x.id === cid);
  const url = `/api/clips/${cid}/file`;
  $("#ovCard").classList.add("hidden"); $("#ovTexts").innerHTML = ""; pv.style.filter = pv.style.transform = "";
  if (pv.dataset.url !== url) { pv.src = url; pv.dataset.url = url; }
  const go = () => { pv.currentTime = t; pv.muted = false; pv.playbackRate = 1; pv.play().catch(() => {}); };
  pv.readyState >= 1 ? go() : pv.addEventListener("loadedmetadata", go, { once: true });
  toast(`Source: ${c?.name} @ ${fmtT(t)} (press ▶ to return to the edit)`);
}
// Cut by editing words: select words in the transcript (drag across them) and press Delete or "Cut words".
function wordSelection() {
  const s = document.getSelection();
  if (!s || s.isCollapsed || !$("#transcript").contains(s.anchorNode)) return null;
  const rows = new Map();
  $$("#transcript .seg").forEach((row) => row.querySelectorAll(".w").forEach((w) => {
    if (s.containsNode(w, true)) (rows.get(row) || rows.set(row, []).get(row)).push(w);
  }));
  return rows.size ? rows : null;
}
async function cutWords() {
  const rows = wordSelection();
  if (!rows) return toast("Select some words in the transcript first.", true);
  const actions = [];
  for (const [row, ws] of rows) {
    const ci = S.project.clips.findIndex((c) => c.id === row.dataset.id);
    actions.push({ type: "cut_words", ids: [`c${ci}.s${row.dataset.i}`], start: +ws[0].dataset.a, end: +ws[ws.length - 1].dataset.b });
  }
  document.getSelection().removeAllRanges();
  await act(actions);
}
$("#cutWordsBtn").onclick = cutWords;
document.addEventListener("selectionchange", () => $("#cutWordsBtn").classList.toggle("hot", !!wordSelection()));
$("#transcript").addEventListener("click", async (e) => {
  const row = e.target.closest(".seg");
  if (!row || !document.getSelection().isCollapsed) return;
  if (e.target.classList.contains("w")) return peekSource(row.dataset.id, +e.target.dataset.a);
  const { id, kind, i } = row.dataset;
  const it = S.project.clips.find((x) => x.id === id)[kind][+i];
  if (e.target.type === "checkbox") {
    try { await patchItem(id, kind, +i, { keep: e.target.checked }); } catch (err) { e.target.checked = !e.target.checked; fail(err); }
  } else peekSource(id, it.start);
});
$("#tracks").addEventListener("pointerdown", (e) => {
  const block = e.target.closest("i[data-kind]");
  if (!block) return;
  const lane = block.parentElement, c = S.project.clips.find((x) => x.id === lane.dataset.id);
  const { kind, i } = block.dataset, it = c[kind][+i];
  if (!e.target.classList.contains("h")) return peekSource(c.id, it.start);
  e.preventDefault();
  const edge = e.target.classList.contains("l") ? "start" : "end";
  const rect = lane.getBoundingClientRect();
  let value = it[edge];
  const move = (ev) => {
    value = Math.max(0, Math.min(c.duration, ((ev.clientX - rect.left) / rect.width) * c.duration));
    if (edge === "start") value = Math.min(value, it.end - 0.2); else value = Math.max(value, it.start + 0.2);
    block.style.left = ((edge === "start" ? value : it.start) / c.duration) * 100 + "%";
    block.style.width = (((edge === "end" ? value : it.end) - (edge === "start" ? value : it.start)) / c.duration) * 100 + "%";
    toast(`${edge === "start" ? "Start" : "End"}: ${fmtT(value)}`);
  };
  const up = async () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    try { await patchItem(c.id, kind, +i, { [edge]: value }); } catch (err) { fail(err); renderTranscript(); }
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});

// ---------------------------------------------------------------- search
$("#searchForm").onsubmit = (e) => { e.preventDefault(); startJob("/api/search", { query: $("#searchInput").value }); };
function renderSearch(r) {
  $("#searchResults").innerHTML = r.hits.map((h) => `<div class="res">
    <img loading="lazy" src="/api/frame?clip_id=${h.clip}&t=${h.t}&w=320" alt="${esc(h.name)} at ${fmtT(h.t)}" data-peek="${h.clip}|${h.t}">
    <div><span>${esc(h.name)} @ ${fmtT(h.t)}</span><button data-addb="${h.clip}|${h.t}" title="Add 5 s around this moment as B-roll">+ B-roll</button></div></div>`).join("") ||
    `<p class="muted">Nothing matched “${esc(r.query)}”.</p>`;
}
$("#searchResults").onclick = (e) => {
  const pk = e.target.dataset.peek, ab = e.target.dataset.addb;
  if (pk) { const [c, t] = pk.split("|"); peekSource(c, +t); }
  if (ab) { const [c, t] = ab.split("|"); act([{ type: "add_broll", value: c, start: Math.max(0, +t - 2.5), end: +t + 2.5, text: `search: ${$("#searchInput").value}` }]); }
};

// ---------------------------------------------------------------- jobs & console
function appendLog(line, isErr) {
  const live = $("#thinking .live");
  if (live) { live.textContent += line.replace(/^\[[\d:]+\]\s*/, "") + "\n"; live.scrollTop = 1e9; $("#chatLog").scrollTop = 1e9; }
  const log = $("#log"), el = document.createElement("div");
  el.textContent = line;
  if (isErr || line.includes("ERROR")) el.className = "err";
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}
function showResult(r) {
  if (!r) return;
  if (r.files) {
    $("#result").innerHTML = `<p>✅ Saved to <code>${esc(r.folder)}</code></p><ul>${r.files.map((f) => `<li><a href="${f.url}" download>${esc(f.name)}</a></li>`).join("")}</ul><button id="reveal">Open output folder</button>`;
    $("#reveal").onclick = () => post("/api/reveal").catch(fail);
    openPane("log");
  }
  if (r.preview) { previewUrl = r.preview; mode = "rendered"; syncMode(); pv.dataset.url = ""; showAt(0, false); toast("Preview rendered: every effect is baked in."); }
  if (r.search) { renderSearch(r.search); openPane("search"); }
  if (r.voice_test) { const a = $("#voiceAudio"); a.src = r.voice_test; a.classList.remove("hidden"); a.play().catch(() => {}); }
  if (r.voice || r.agent) loadState();
  if (r.update) { $("#updateStatus").textContent = r.update.message; $("#restartUpdate").classList.toggle("hidden", !r.update.restart); }
}
async function poll() {
  try {
    const j = await api(`/api/job?since=${logCount}`);
    if (j.log_count < logCount) { logCount = 0; $("#log").innerHTML = ""; return; }
    j.logs.forEach((l) => appendLog(l));
    logCount = j.log_count;
    if (j.running || wasRunning) {
      $("#bar").style.width = j.progress * 100 + "%";
      $("#stepLabel").className = "small muted";
      $("#stepLabel").textContent = j.running ? j.step : j.error ? (j.error === "Cancelled" ? "Cancelled" : "Failed: " + j.error.slice(0, 80)) : "Done";
    }
    ["#runBtn", "#exportBtn", "#runQueue", "#previewBtn"].forEach((s) => ($(s).disabled = j.running));
    $("#cancelBtn").classList.toggle("hidden", !j.running);
    if (wasRunning && !j.running) {
      if (!j.error) showResult(j.result);
      else if (j.error !== "Cancelled") { appendLog("ERROR: " + j.error, true); }
      if (j.error && $("#thinking .live")) await loadState();  // the agent's failure is in the chat
      if (["AI edit", "Sync cameras"].includes(j.name) || j.name.startsWith("Batch") || j.name.startsWith("Create voice")) await loadState();
      if (!$("#settings").classList.contains("hidden")) loadSetup();
    }
    wasRunning = j.running;
  } catch { /* server restarting — keep polling */ }
}
async function startJob(path, body) {
  try { logCount = 0; $("#log").innerHTML = ""; await post(path, body); wasRunning = true; poll(); }
  catch (e) { fail(e); }
}
$("#runBtn").onclick = () => startJob("/api/run");
$("#exportBtn").onclick = () => startJob("/api/export");
$("#cancelBtn").onclick = () => { $("#cancelBtn").disabled = true; post("/api/cancel").finally(() => ($("#cancelBtn").disabled = false)); };

// ---------------------------------------------------------------- settings page
async function loadSetup() {
  const s = await api("/api/setup");
  $("#ffmpegStatus").innerHTML = s.ffmpeg ? `FFmpeg <span class="ok">✓</span> <code>${esc(s.ffmpeg)}</code>` : `<span class="err">✗ ${esc(s.ffmpeg_error)}</span>`;
  $("#whisperList").innerHTML = s.whisper_error ? `<li class="err">${esc(s.whisper_error)}</li>` :
    Object.entries(s.whisper).map(([size, ok]) => `<li><b style="width:110px">${size}</b>${ok ? '<span class="ok">✓ downloaded</span>' : `<button data-size="${size}">Download</button>`}</li>`).join("");
  api("/api/encoder").then((r) => ($("#encoderInfo").textContent = r.hardware ? `Hardware encoder on this machine: ${r.hardware}` : "No hardware encoder found; Auto uses libx264."));
  api("/api/voice").then((v) => {
    const av = v.available || {};
    $("#voiceStatus").textContent = v.error || `Engines: ${["chatterbox", "server", "system"].map((e) => `${e} ${av[e] ? "✓" : "✗"}`).join(" · ")}`;
  }).catch(() => {});
}
$("#whisperList").onclick = (e) => {
  if (!e.target.dataset.size) return;
  e.target.disabled = true;
  e.target.textContent = "Downloading… (see the Log tab)";
  startJob(`/api/setup/whisper/${e.target.dataset.size}`);
};
$("#testLlm").onclick = async () => { $("#setupLlm").textContent = await refreshModels(); };
async function setPath(key, value) { $(`[data-setting="${key}"]`).value = value; await saveSettings({ [key]: value }); }
$("#pickFolder").onclick = async () => { if (!desktop()) return $("#output_dir").focus(); const f = await desktop().pick_folder(); if (f) setPath("output_dir", f); };
$("#resetFolder").onclick = () => setPath("output_dir", "");
$("#pickMusic").onclick = async () => { if (!desktop()) return $("#music_path").focus(); const f = await desktop().pick_audio(); if (f) setPath("music_path", f); };
$("#clearMusic").onclick = () => setPath("music_path", "");
$("#voiceFromProject").onclick = () => startJob("/api/voice/sample", { consent: $("#voiceConsent").checked });
$("#voicePick").onclick = async () => {
  if (!desktop()) { const p = prompt("Path to a 10–20 s recording of your voice (.wav/.mp3):"); if (!p) return; return post("/api/voice/sample", { consent: $("#voiceConsent").checked, path: p }).then(loadState).catch(fail); }
  const f = await desktop().pick_audio();
  if (f) post("/api/voice/sample", { consent: $("#voiceConsent").checked, path: f }).then(loadState).catch(fail);
};
$("#voiceTestForm").onsubmit = (e) => { e.preventDefault(); startJob("/api/voice/test", { text: $("#voiceTestText").value }); };

// ---------------------------------------------------------------- updates
async function checkUpdate(force) {
  $("#updateStatus").textContent = "Checking…";
  const u = await api(`/api/update${force ? "?force=1" : ""}`).catch((e) => ({ error: e.message }));
  update = u;
  $("#updateStatus").textContent = u.disabled ? "Automatic checks are off" : u.error ? u.error
    : u.newer ? `Version ${u.latest} is available` : `You're up to date (latest is ${u.latest})`;
  $("#installUpdate").classList.toggle("hidden", !(u.newer && u.can_install));
  $("#downloadUpdate").classList.toggle("hidden", !(u.newer && !u.can_install));
  const b = $("#updateBanner");
  b.classList.toggle("hidden", !u.newer);
  if (u.newer) {
    b.innerHTML = `LeanCut ${esc(u.latest)} is out. ${u.can_install ? '<button id="bannerInstall" class="primary">Install</button>' : '<button id="bannerGet">Download</button>'} <button id="bannerNotes">What's new</button> <button id="bannerClose" aria-label="Dismiss">✕</button>`;
    $("#bannerGet") && ($("#bannerGet").onclick = () => openUrl(u.url));
    $("#bannerInstall") && ($("#bannerInstall").onclick = () => { showTab("editor"); startJob("/api/update/install"); });
    $("#bannerNotes").onclick = () => openUrl(u.notes_url);
    $("#bannerClose").onclick = () => b.classList.add("hidden");
  }
}
const openUrl = (url) => post("/api/open-url", { url }).catch(fail);
$("#checkUpdate").onclick = () => checkUpdate(true);
$("#downloadUpdate").onclick = () => update && openUrl(update.url);
$("#installUpdate").onclick = () => startJob("/api/update/install");
$("#restartUpdate").onclick = () => post("/api/update/restart").then(() => toast("Restarting LeanCut…")).catch(fail);

loadState().then(() => {
  refreshModels();
  loadAgents();
  loadTemplates();
  if (S.settings.update_check) checkUpdate(false);
  // deep links: #settings, #projects, #part=3, #pane=sugg (combine with &)
  for (const kv of location.hash.slice(1).split("&")) {
    const [k, v] = kv.split("=");
    if (["projects", "settings"].includes(k)) showTab(k);
    if (k === "part" && plan.parts[+v - 1]) select(+v - 1, true);
    if (k === "pane" && $(`.t2[data-pane="${v}"]`)) openPane(v);
  }
}).catch(fail);
setInterval(poll, 1000);
poll();
