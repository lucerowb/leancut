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
    stop(); T = 0; sel = -1; previewUrl = null; mode = "instant"; syncMode();
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
      <span class="muted small">${fmtT(c.duration)} · ${fmtSize(c.size)} · ${c.width}x${c.height}${c.segments ? " · transcribed" : ""}</span>
      ${sync}
      <span class="extra"><button data-act="gyro" title="Use the gyro-stabilised file Gyroflow exported next to this clip">${c.original_path ? "Use original" : "Use Gyroflow export"}</button></span>
    </li>`;
  }).join("") || '<li class="muted">No clips yet</li>';
  $("#ovEmpty").classList.toggle("hidden", !!plan.parts.length);
}
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
const pv = $("#pv");
const introDur = () => +(plan.intro?.duration || 0);
const outroStart = () => plan.parts.length ? plan.parts[plan.parts.length - 1].rec_end : introDur();
async function loadPlan() {
  plan = await api("/api/plan");
  const kept = plan.parts.filter((p) => !p.silent).reduce((a, p) => a + (p.rec_end - p.rec_start), 0);
  $("#summary").textContent = plan.parts.length ? `${plan.parts.length} parts · ${fmtT(plan.duration)} total${S.project.target_length ? " · target " + S.project.target_length : ""}` : "";
  $("#ovEmpty").classList.toggle("hidden", !!plan.parts.length);
  if (sel >= plan.parts.length) sel = plan.parts.length - 1;
  T = Math.min(T, plan.duration);
  renderTimeline();
  renderInspector();
  if (!playing) await showAt(T, false);
  return kept;
}
function partAt(t) {
  if (t < introDur()) return "intro";
  if (t >= outroStart()) return plan.outro && t < plan.duration ? "outro" : -1;
  return plan.parts.findIndex((p) => t >= p.rec_start && t < p.rec_end);
}
const srcUrl = (p) => `/api/clips/${p.clip}/file${p.chapter != null ? "?chapter=" + p.chapter : ""}`;
const once = (el, ev) => new Promise((res) => el.addEventListener(ev, res, { once: true }));

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
async function showAt(t, play) {
  T = Math.max(0, Math.min(t, plan.duration || 0));
  peek = false;
  if (mode === "rendered" && previewUrl) {
    $("#ovCard").classList.add("hidden");
    if (pv.dataset.url !== previewUrl) { pv.src = previewUrl; pv.dataset.url = previewUrl; await once(pv, "loadedmetadata"); }
    pv.style.filter = pv.style.transform = ""; $("#ovVignette").style.opacity = 0; $("#ovTexts").innerHTML = "";
    pv.currentTime = T; pv.playbackRate = 1; pv.muted = false;
    if (play) pv.play().catch(() => {});
    return draw();
  }
  const k = partAt(T);
  if (k === "intro" || k === "outro") { pv.pause(); card(k); cur = k; return draw(); }
  $("#ovCard").classList.add("hidden");
  if (k < 0) { cur = -1; return draw(); }
  const p = plan.parts[k];
  cur = k;
  const url = srcUrl(p);
  if (pv.dataset.url !== url) { pv.src = url; pv.dataset.url = url; await once(pv, "loadedmetadata"); }
  pv.currentTime = p.src_start + (T - p.rec_start) * p.speed;
  pv.playbackRate = Math.min(16, p.speed);
  pv.muted = !!p.silent;
  pv.style.filter = gradeCss(p.grade, p.effect);
  $("#ovVignette").style.opacity = Math.max(p.grade?.vignette || 0, p.effect === "vignette" ? 0.8 : 0);
  if (play) await pv.play().catch(() => {});
  draw();
}
function overlays() {
  const box = $("#ovTexts");
  if (mode === "rendered" || typeof cur !== "number" || cur < 0) { box.innerHTML = ""; return; }
  const p = plan.parts[cur], lt = T - p.rec_start, dur = p.rec_end - p.rec_start;
  const scale = p.effect === "zoom" ? 1.15 : p.effect === "push_in" ? 1 + 0.12 * Math.min(1, lt / dur) : 1;
  pv.style.transform = scale !== 1 ? `scale(${scale.toFixed(4)})` : "";
  const html = p.texts.filter((x) => lt >= x.t0 && lt < x.t1).map((x) => `<div class="tx ${x.style}">${esc(x.text)}</div>`);
  html.push(...p.captions.filter((x) => lt >= x.t0 && lt < x.t1).map((x) => `<div class="tx caption"><span>${esc(x.text)}</span></div>`));
  box.innerHTML = html.join("");
  const next = plan.parts[cur + 1];  // approximate transitions as a short dip
  const into = p.transition && lt < 0.25 ? 1 - lt / 0.25 : 0;
  const out = next?.transition && dur - lt < 0.25 ? 1 - (dur - lt) / 0.25 : 0;
  $("#ovDim").style.opacity = Math.max(into, out) * 0.7;
}
function draw() {
  overlays();
  $("#tc").textContent = `${fmtT(T)} / ${fmtT(plan.duration)}`;
  const pps = pxPerSec();
  $("#playhead").style.left = 64 + T * pps + "px";
  $("#playBtn").textContent = playing ? "❚❚" : "▶";
  const k = partAt(T);
  $$("#laneV .blk").forEach((b) => b.classList.toggle("sel", +b.dataset.i === sel));
}
let lastFrame = 0;
function loop(ts) {
  const dt = lastFrame ? (ts - lastFrame) / 1000 : 0;
  lastFrame = ts;
  if (playing && !peek) {
    if (mode === "rendered") { T = pv.currentTime; if (pv.ended) stop(); }
    else if (cur === "intro" || cur === "outro") {
      T += dt;
      if (cur === "intro" && T >= introDur()) showAt(T, true);
      if (cur === "outro" && T >= plan.duration) { stop(); }
    } else if (typeof cur === "number" && cur >= 0) {
      const p = plan.parts[cur];
      if (!pv.seeking) T = p.rec_start + (pv.currentTime - p.src_start) / p.speed;
      if (pv.currentTime >= p.src_end - 0.04 || pv.ended) {
        T = p.rec_end + 0.001;
        if (T >= plan.duration - 0.01 && !plan.outro) stop(); else showAt(T, true);
      }
    } else stop();
    draw();
  }
  requestAnimationFrame(loop);
}
requestAnimationFrame(loop);
function play() { if (!plan.parts.length) return; playing = true; if (T >= plan.duration - 0.05) T = 0; showAt(T, true); }
function stop() { playing = false; pv.pause(); draw(); }
$("#playBtn").onclick = () => (playing ? stop() : play());
function syncMode() {
  $("#modeInstant").classList.toggle("on", mode === "instant");
  $("#modeRendered").classList.toggle("on", mode === "rendered");
  $("#modeRendered").disabled = !previewUrl;
}
$("#modeInstant").onclick = () => { mode = "instant"; syncMode(); pv.dataset.url = ""; showAt(T, playing); };
$("#modeRendered").onclick = () => { if (!previewUrl) return; mode = "rendered"; syncMode(); showAt(T, playing); };
$("#previewBtn").onclick = () => startJob("/api/preview");

// ---------------------------------------------------------------- timeline
function pxPerSec() {
  const w = Math.max(400, $("#timeline").clientWidth - 70);
  return (w / Math.max(plan.duration, 1)) * +$("#zoom").value;
}
const isBroll = (p) => p.ids.length && p.ids.every((i) => i.includes(".b"));
function label(p) {
  if (p.silent) return `⏩ ${p.speed.toFixed(0)}×`;
  const spoken = p.ids.map(item).find((x) => x?.text);
  const it = item(p.ids[0]);
  return spoken?.text || ("B-roll" + (it?.reason ? " · " + it.reason : ""));
}
function renderTimeline() {
  const pps = pxPerSec(), W = plan.duration * pps;
  ["#laneV", "#laneT", "#laneA"].forEach((s) => ($(s).style.width = W + "px"));
  $("#ruler").style.width = W + "px";
  const step = [1, 2, 5, 10, 15, 30, 60, 120, 300].find((s) => s * pps >= 60) || 600;
  let ticks = "";
  for (let t = 0; t <= plan.duration; t += step) ticks += `<span style="left:${t * pps}px">${fmtT(t).replace(/\.\d$/, "")}</span>`;
  $("#ruler").innerHTML = ticks;
  const blk = (cls, x, w, inner, attrs = "") => `<div class="blk ${cls}" style="left:${x}px;width:${Math.max(3, w - 1)}px" ${attrs}>${inner}</div>`;
  let v = "";
  if (plan.intro) v += blk("card", 0, introDur() * pps, "Intro · " + esc(plan.intro.title), 'data-card="intro"');
  plan.parts.forEach((p, i) => {
    const fx = [p.effect !== "none" && p.effect, p.speed !== 1 && !p.silent && `${p.speed}×`].filter(Boolean);
    v += blk(`${isBroll(p) ? "broll" : ""} ${p.silent ? "ff" : ""}`, p.rec_start * pps, (p.rec_end - p.rec_start) * pps,
      `${p.transition ? '<span class="tr" title="' + esc(p.transition.type) + '"></span>' : ""}${esc(label(p))}${fx.map((f) => `<span class="fx">${esc(f)}</span>`).join("")}`,
      `data-i="${i}" title="${esc(label(p))}"`);
  });
  if (plan.outro) v += blk("card", outroStart() * pps, (plan.duration - outroStart()) * pps, "Outro · " + esc(plan.outro.title), 'data-card="outro"');
  $("#laneV").innerHTML = v;
  $("#laneT").innerHTML = plan.parts.flatMap((p, i) => p.texts.map((x) => blk("text", (p.rec_start + x.t0) * pps, (x.t1 - x.t0) * pps, esc(x.text), `data-i="${i}"`))).join("");
  $("#laneA").innerHTML = plan.parts.flatMap((p, i) => p.voiceovers.map((x) => blk("vo", (p.rec_start + x.t0) * pps, Math.max(2, x.text.split(" ").length / 2.6) * pps, "🎙 " + esc(x.text), `data-i="${i}"`))).join("");
  draw();
}
$("#zoom").oninput = renderTimeline;
window.addEventListener("resize", () => renderTimeline());
$("#timeline").addEventListener("click", (e) => {
  const b = e.target.closest(".blk");
  if (b?.dataset.i != null) { select(+b.dataset.i, true); return; }
  if (b?.dataset.card) { showAt(b.dataset.card === "intro" ? 0 : outroStart(), playing); openPane("look"); return; }
  const r = $("#laneV").getBoundingClientRect();
  if (e.clientX >= r.left) showAt((e.clientX - r.left) / pxPerSec(), playing);
});
function select(i, seek) {
  sel = i;
  renderInspector();
  openPane("insp");
  if (seek && plan.parts[i]) showAt(plan.parts[i].rec_start + 0.01, playing);
  draw();
}
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea, select") || $("#editor").classList.contains("hidden")) return;
  const mod = e.metaKey || e.ctrlKey;
  if (e.code === "Space") { e.preventDefault(); playing ? stop() : play(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); select(Math.min(plan.parts.length - 1, sel + 1), true); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); select(Math.max(0, sel - 1), true); }
  else if ((e.key === "Delete" || e.key === "Backspace") && sel >= 0 && plan.parts[sel]?.ids.length) { e.preventDefault(); act([{ type: "cut", ids: plan.parts[sel].ids }]); }
  else if (mod && e.key.toLowerCase() === "z") { e.preventDefault(); (e.shiftKey ? $("#redoBtn") : $("#undoBtn")).click(); }
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
  const p = plan.parts[sel];
  if (!p) { box.innerHTML = '<p class="muted">Select a part on the timeline.</p>'; return; }
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
      <div class="row wrap"><button id="iPlay">▶ Play from here</button>${p.ids.length ? '<button id="iCut">Cut this part</button>' : ""}</div>
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
  $("#iPlay").onclick = () => { showAt(p.rec_start + 0.01, true); playing = true; };
  $("#iCut") && ($("#iCut").onclick = () => act([{ type: "cut", ids: p.ids }]));
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
  { type: "intro", text: $("#introTitle").value, value: $("#introSub").value, style: $("#introStyle").value, duration: 4 },
  { type: "outro", text: $("#outroTitle").value, value: $("#outroSub").value, style: $("#introStyle").value, duration: 4 }]);
$("#captions").onchange = (e) => act([{ type: "captions", value: e.target.checked ? "on" : "off" }]);
$("#privacy").onchange = (e) => act([{ type: "privacy_blur", value: e.target.checked ? "on" : "off" }]);
$("#brollShare").onchange = (e) => act([{ type: "broll_share", amount: +e.target.value }]);
$("#musicVol").onchange = (e) => act([{ type: "music_volume", amount: +e.target.value }]);

// ---------------------------------------------------------------- assistant: chat & suggestions
function renderChat() {
  const msgs = S.project.chat || [];
  $("#chatLog").innerHTML = (msgs.length ? "" : '<p class="muted small">Tell me what to change, in your own words. Every change can be undone.</p>') +
    msgs.map((m) => `<div class="msg ${m.role}">${esc(m.text)}${m.done?.length ? `<ul>${m.done.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>` : ""}${m.errors?.length ? `<div class="errs">${m.errors.map(esc).join("<br>")}</div>` : ""}</div>`).join("");
  $("#chatLog").scrollTop = 1e9;
}
$("#chips").onclick = (e) => { if (e.target.tagName === "BUTTON") { $("#chatInput").value = e.target.textContent; $("#chatInput").focus(); } };
$("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chatForm").requestSubmit(); } });
$("#chatForm").onsubmit = async (e) => {
  e.preventDefault();
  const message = $("#chatInput").value.trim();
  if (!message) return;
  $("#chatInput").value = "";
  $("#chatLog").insertAdjacentHTML("beforeend", `<div class="msg user">${esc(message)}</div><div class="msg assistant muted" id="thinking">Thinking…</div>`);
  $("#chatLog").scrollTop = 1e9;
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
      const lbl = s.kind === "broll" ? `Scenic B-roll · ${d.toFixed(1)} s` : esc(s.text);
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
$("#transcript").addEventListener("click", async (e) => {
  const row = e.target.closest(".seg");
  if (!row) return;
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
  if (r.voice) loadState();
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
