const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const DEFAULT_URLS = { ollama: "http://localhost:11434", lmstudio: "http://localhost:1234/v1" };
let S = { project: { clips: [] }, projects: [], settings: {}, sizes: [], languages: {} };
let current = null, logCount = 0, wasRunning = false, update = null;
const desktop = () => window.pywebview?.api;

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmtT = (t) => { t = Math.max(0, t); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = (t % 60).toFixed(1).padStart(4, "0"); return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + s; };
const fmtSize = (b) => (b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(0) + " MB");

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
const post = (path, body, method = "POST") => api(path, { method, body: JSON.stringify(body || {}) });
const fail = (e) => appendLog("ERROR: " + e.message, true);

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== name));
  if (name === "settings") loadSetup();
}
$$(".tab").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
$$("[data-goto]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); showTab(a.dataset.goto); }));

// ---------------------------------------------------------------- state & settings
async function loadState() {
  S = await api("/api/state");
  if (!$("#whisper_size").options.length) S.sizes.forEach((s) => $("#whisper_size").add(new Option(s, s)));
  if (!$("#language").options.length) Object.entries(S.languages).forEach(([c, n]) => $("#language").add(new Option(n, c)));
  $$("[data-setting]").forEach((el) => {
    const v = S.settings[el.dataset.setting];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.id !== "model") el.value = v ?? "";
  });
  $("#target_length").value = S.project.target_length || "";
  $("#brollShareOut").textContent = S.settings.broll_share + "%";
  $("#version").textContent = "v" + S.version;
  $("#defaultOutput").textContent = S.default_output;
  $("#dataDir").textContent = S.data_dir;
  $("#projectSelect").innerHTML = S.projects.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("") + '<option value="__new">+ New project…</option>';
  $("#projectSelect").value = S.project.id;
  if (!current && S.project.clips.length) {  // show the first clip instead of an empty player
    current = S.project.clips[0].id;
    $("#player").src = `/api/clips/${current}/file#t=1`;
    $("#nowPlaying").textContent = S.project.clips[0].name;
  }
  renderClips();
  renderTimeline();
  renderProjects();
}

const saveSettings = (patch) => post("/api/settings", patch).then((s) => (S.settings = s)).catch(fail);
$$("[data-setting]").forEach((el) => el.addEventListener("change", async () => {
  const k = el.dataset.setting;
  const v = el.type === "checkbox" ? el.checked : el.value;
  if (k === "provider") { $("#base_url").value = DEFAULT_URLS[v]; await saveSettings({ provider: v, base_url: DEFAULT_URLS[v] }); return refreshModels(); }
  await saveSettings({ [k]: v });
  if (k === "base_url") refreshModels();
}));
$("#broll_share").addEventListener("input", (e) => ($("#brollShareOut").textContent = e.target.value + "%"));
$("#target_length").addEventListener("change", async (e) => { await saveSettings({ target_length: e.target.value }); S.project.target_length = e.target.value; renderTimeline(); });

async function refreshModels() {
  const sel = $("#model"), status = $("#llmStatus");
  status.textContent = "Connecting…";
  status.className = "muted small";
  try {
    const { models } = await api(`/api/llm/models?provider=${encodeURIComponent(S.settings.provider)}&base_url=${encodeURIComponent(S.settings.base_url)}`);
    sel.innerHTML = "";
    models.forEach((m) => sel.add(new Option(m, m)));
    if (models.includes(S.settings.model)) sel.value = S.settings.model;
    else if (models.length) saveSettings({ model: models[0] });
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
      const name = prompt("Name of the new project:", "New ride");  // the native window shows real dialogs
      if (name === null) return ($("#projectSelect").value = S.project.id);
      await post("/api/projects", { name });
    } else await post(`/api/projects/${e.target.value}/open`);
    current = null;
    $("#player").removeAttribute("src");
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
  const act = e.target.dataset.act, id = e.target.closest("tr")?.dataset.id;
  if (!act) return;
  const p = S.projects.find((x) => x.id === id);
  try {
    if (act === "open") { await post(`/api/projects/${id}/open`); current = null; await loadState(); showTab("editor"); }
    if (act === "rename") { const name = prompt("New name:", p.name); if (name) { await post(`/api/projects/${id}`, { name }, "PATCH"); await loadState(); } }
    if (act === "delete" && confirm(`Remove “${p.name}” from LeanCut? Its folder is moved aside, not deleted, and your original footage is never touched.`)) {
      await api(`/api/projects/${id}`, { method: "DELETE" }); await loadState();
    }
  } catch (err) { fail(err); }
};
$("#newProject").onsubmit = async (e) => {
  e.preventDefault();
  try { await post("/api/projects", { name: $("#newProjectName").value }); $("#newProjectName").value = ""; current = null; await loadState(); showTab("editor"); } catch (err) { fail(err); }
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
        ${others.map((m) => `<option value="${m.id}" ${c.angle_of === m.id ? "selected" : ""}>Second angle of ${esc(m.name)}</option>`).join("")}
      </select>${c.angle_of ? ` ${c.sync_offset >= 0 ? "+" : ""}${c.sync_offset.toFixed(2)} s` : ""}</span>` : "";
    return `<li data-id="${c.id}" class="${current === c.id ? "sel" : ""}">
      <span class="name" title="${esc(c.path)}">${i + 1}. ${esc(c.name)}</span>
      <span class="btns">
        <button data-act="up" aria-label="Move up">↑</button><button data-act="down" aria-label="Move down">↓</button>
        <button data-act="del" aria-label="Remove clip">✕</button>
      </span>
      <span class="muted small">${fmtT(c.duration)} · ${fmtSize(c.size)} · ${c.width}x${c.height}${c.segments ? " · transcribed" : ""}</span>
      ${sync}
    </li>`;
  }).join("") || '<li class="muted">No clips yet</li>';
}

$("#clips").onclick = async (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li || e.target.tagName === "SELECT" || e.target.tagName === "OPTION") return;
  const act = e.target.dataset.act, id = li.dataset.id;
  try {
    if (act === "del") { if (!confirm("Remove this clip from the project? The file itself is not deleted.")) return; await api(`/api/clips/${id}`, { method: "DELETE" }); }
    else if (act) await post(`/api/clips/${id}/move`, { delta: act === "up" ? -1 : 1 });
    else return preview(id, 0);
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

function preview(id, t) {
  const c = S.project.clips.find((x) => x.id === id), v = $("#player");
  if (current !== id) { v.src = `/api/clips/${id}/file`; current = id; renderClips(); }
  $("#nowPlaying").textContent = `${c.name} @ ${fmtT(t)}`;
  const go = () => { v.currentTime = t; v.play().catch(() => {}); };
  v.readyState >= 1 ? go() : v.addEventListener("loadedmetadata", go, { once: true });
}

function upload(files) {
  if (!files.length) return;
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/clips/upload");
  xhr.upload.onprogress = (e) => { $("#stepLabel").textContent = `Uploading… ${Math.round((e.loaded / e.total) * 100)}%`; $("#bar").style.width = (e.loaded / e.total) * 100 + "%"; };
  xhr.onload = () => {
    $("#stepLabel").textContent = "Idle";
    if (xhr.status >= 400) fail(new Error(JSON.parse(xhr.responseText || "{}").detail || "Upload failed"));
    else appendLog(`Added ${files.length} clip(s).`);
    loadState();
  };
  xhr.onerror = () => fail(new Error("Upload failed"));
  xhr.send(fd);
}
const drop = $("#drop");
$("#files").onchange = (e) => upload(e.target.files);
// Desktop app: the native picker gives real paths, so clips are referenced in place instead of copied.
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
window.addEventListener("pywebviewready", () => ($("#dropHint").textContent = "Picked files are used where they are; dropped files are copied"));

// ---------------------------------------------------------------- timeline & transcript
const items = (c) => [
  ...(c.segments || []).map((x, i) => ({ ...x, kind: "segments", i })),
  ...(c.broll || []).map((x, i) => ({ ...x, kind: "broll", i })),
].sort((a, b) => a.start - b.start);

function renderTimeline() {
  let kept = 0, speech = 0, broll = 0, total = 0;
  const tracks = [], text = [];
  S.project.clips.forEach((c, ci) => {
    total += c.angle_of ? 0 : c.duration;
    const all = items(c);
    const label = c.angle_of ? ` <span class="angle">· second angle, follows its main clip in FCPXML/OTIO</span>` : "";
    tracks.push(`<div class="track"><div class="lbl">${ci + 1}. ${esc(c.name)}${label}</div><div class="lane" data-id="${c.id}">${all.map((s) =>
      `<i class="${s.keep ? "k" : ""} ${s.kind === "broll" ? "b" : ""}" data-kind="${s.kind}" data-i="${s.i}" title="${esc(s.text || s.reason || "")}"
        style="left:${(s.start / c.duration) * 100}%;width:${Math.max(0.15, ((s.end - s.start) / c.duration) * 100)}%">${s.keep ? '<b class="h l"></b><b class="h r"></b>' : ""}</i>`).join("")}</div></div>`);
    if (!all.length) return;
    text.push(`<h3>${ci + 1}. ${esc(c.name)}</h3>`);
    all.forEach((s) => {
      const d = s.end - s.start;
      if (s.kind === "segments") { speech += d; if (s.keep) kept += d; } else if (s.keep) broll += d;
      const label = s.kind === "broll" ? `Scenic B-roll · ${d.toFixed(1)} s` : esc(s.text);
      const why = s.kind === "broll" ? s.reason : (s.score ? `★${s.score} ${s.reason || ""}` : "");
      text.push(`<div class="seg ${s.keep ? "k" : ""} ${s.kind === "broll" ? "broll" : ""}" data-id="${c.id}" data-kind="${s.kind}" data-i="${s.i}">
        <input type="checkbox" ${s.keep ? "checked" : ""} aria-label="Keep">
        <span class="t">${fmtT(s.start)}</span>
        <span class="txt">${label}${why ? `<span class="why">${esc(why)}</span>` : ""}</span></div>`);
    });
  });
  $("#tracks").innerHTML = tracks.join("");
  $("#transcript").innerHTML = text.join("") || '<p class="muted">Run “Auto-edit with AI” to transcribe and get the AI’s cut.</p>';
  const target = S.project.target_length ? ` · target ${esc(S.project.target_length)}` : "";
  $("#summary").innerHTML = total ? `Kept <b>${fmtT(kept + broll)}</b> (${fmtT(kept)} talking${broll ? ` + ${fmtT(broll)} B-roll` : ""}) of ${fmtT(total)} raw${target}` : "";
}

async function patchItem(cid, kind, i, body) {
  const c = S.project.clips.find((x) => x.id === cid);
  Object.assign(c[kind][i], await api(`/api/clips/${cid}/${kind}/${i}`, { method: "PATCH", body: JSON.stringify(body) }));
  renderTimeline();
}

$("#transcript").addEventListener("click", async (e) => {
  const row = e.target.closest(".seg");
  if (!row) return;
  const { id, kind, i } = row.dataset;
  const item = S.project.clips.find((x) => x.id === id)[kind][+i];
  if (e.target.type === "checkbox") {
    try { await patchItem(id, kind, +i, { keep: e.target.checked }); } catch (err) { e.target.checked = !e.target.checked; fail(err); }
  } else preview(id, item.start);
});

// Drag the edges of a kept block to trim it; click a block to preview it.
$("#tracks").addEventListener("pointerdown", (e) => {
  const block = e.target.closest("i[data-kind]");
  if (!block) return;
  const lane = block.parentElement, c = S.project.clips.find((x) => x.id === lane.dataset.id);
  const { kind, i } = block.dataset, item = c[kind][+i];
  if (!e.target.classList.contains("h")) return preview(c.id, item.start);
  e.preventDefault();
  const edge = e.target.classList.contains("l") ? "start" : "end";
  const rect = lane.getBoundingClientRect();
  let value = item[edge];
  const move = (ev) => {
    value = Math.max(0, Math.min(c.duration, ((ev.clientX - rect.left) / rect.width) * c.duration));
    if (edge === "start") value = Math.min(value, item.end - 0.2); else value = Math.max(value, item.start + 0.2);
    block.style.left = ((edge === "start" ? value : item.start) / c.duration) * 100 + "%";
    block.style.width = (((edge === "end" ? value : item.end) - (edge === "start" ? value : item.start)) / c.duration) * 100 + "%";
    $("#stepLabel").textContent = `${edge === "start" ? "Start" : "End"}: ${fmtT(value)}`;
  };
  const up = async () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    try { await patchItem(c.id, kind, +i, { [edge]: value }); preview(c.id, edge === "start" ? value : Math.max(item.start, value - 2)); } catch (err) { fail(err); renderTimeline(); }
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});

// ---------------------------------------------------------------- jobs & console
function appendLog(line, isErr) {
  const log = $("#log"), el = document.createElement("div");
  el.textContent = line;
  if (isErr || line.includes("ERROR")) el.className = "err";
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function showResult(r) {
  if (!r || !r.files) return;
  $("#result").innerHTML = `<p>✅ Saved to <code>${esc(r.folder)}</code></p><ul>${r.files.map((f) =>
    `<li><a href="${f.url}" download>${esc(f.name)}</a></li>`).join("")}</ul><button id="reveal">Open output folder</button>`;
  $("#reveal").onclick = () => post("/api/reveal").catch(fail);
}

async function poll() {
  try {
    const j = await api(`/api/job?since=${logCount}`);
    if (j.log_count < logCount) { logCount = 0; $("#log").innerHTML = ""; return; } // a new job started
    j.logs.forEach((l) => appendLog(l));
    logCount = j.log_count;
    if (j.running || wasRunning) {
      $("#bar").style.width = j.progress * 100 + "%";
      $("#stepLabel").textContent = j.running ? j.step : j.error ? (j.error === "Cancelled" ? "Cancelled" : "Failed") : "Done";
    }
    $("#runBtn").disabled = $("#exportBtn").disabled = $("#runQueue").disabled = j.running;
    $("#cancelBtn").classList.toggle("hidden", !j.running);
    if (wasRunning && !j.running) {
      await loadState();
      showResult(j.result);
      if (!$("#settings").classList.contains("hidden")) loadSetup();
    }
    wasRunning = j.running;
  } catch { /* server restarting — keep polling */ }
}

async function startJob(path, body) {
  try { logCount = 0; $("#log").innerHTML = ""; $("#result").innerHTML = ""; await post(path, body); wasRunning = true; poll(); }
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
    Object.entries(s.whisper).map(([size, ok]) => `<li><b style="width:110px">${size}</b>${ok ? '<span class="ok">✓ downloaded</span>' :
      `<button data-size="${size}">Download</button>`}</li>`).join("");
  api("/api/encoder").then((r) => ($("#encoderInfo").textContent = r.hardware ? `Hardware encoder on this machine: ${r.hardware}` : "No hardware encoder found; Auto uses libx264."));
}
$("#whisperList").onclick = (e) => {
  if (!e.target.dataset.size) return;
  e.target.disabled = true;
  e.target.textContent = "Downloading… (see Editor console)";
  startJob(`/api/setup/whisper/${e.target.dataset.size}`);
};
$("#testLlm").onclick = async () => { $("#setupLlm").textContent = await refreshModels(); };

async function setPath(key, value) { $(`[data-setting="${key}"]`).value = value; await saveSettings({ [key]: value }); }
$("#pickFolder").onclick = async () => {
  if (!desktop()) return $("#output_dir").focus();  // browser mode: type the path
  const folder = await desktop().pick_folder();
  if (folder) setPath("output_dir", folder);
};
$("#resetFolder").onclick = () => setPath("output_dir", "");
$("#pickMusic").onclick = async () => {
  if (!desktop()) return $("#music_path").focus();
  const file = await desktop().pick_audio();
  if (file) setPath("music_path", file);
};
$("#clearMusic").onclick = () => setPath("music_path", "");

// ---------------------------------------------------------------- updates
async function checkUpdate(force) {
  $("#updateStatus").textContent = "Checking…";
  const u = await api(`/api/update${force ? "?force=1" : ""}`).catch((e) => ({ error: e.message }));
  update = u;
  $("#updateStatus").textContent = u.disabled ? "Automatic checks are off" : u.error ? u.error
    : u.newer ? `Version ${u.latest} is available` : `You're up to date (latest is ${u.latest})`;
  $("#downloadUpdate").classList.toggle("hidden", !u.newer);
  const b = $("#updateBanner");
  b.classList.toggle("hidden", !u.newer);
  if (u.newer) {
    b.innerHTML = `LeanCut ${esc(u.latest)} is out. <button id="bannerGet">Download</button> <button id="bannerNotes">What's new</button> <button id="bannerClose" aria-label="Dismiss">✕</button>`;
    $("#bannerGet").onclick = () => openUrl(u.url);
    $("#bannerNotes").onclick = () => openUrl(u.notes_url);
    $("#bannerClose").onclick = () => b.classList.add("hidden");
  }
}
const openUrl = (url) => post("/api/open-url", { url }).catch(fail);
$("#checkUpdate").onclick = () => checkUpdate(true);
$("#downloadUpdate").onclick = () => update && openUrl(update.url);

loadState().then(() => {
  refreshModels();
  if (S.settings.update_check) checkUpdate(false);
  if (["projects", "settings"].includes(location.hash.slice(1))) showTab(location.hash.slice(1));
}).catch(fail);
setInterval(poll, 1000);
poll();
