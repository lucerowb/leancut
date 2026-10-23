const $ = (s) => document.querySelector(s);
const DEFAULT_URLS = { ollama: "http://localhost:11434", lmstudio: "http://localhost:1234/v1" };
let S = { clips: [], settings: {}, sizes: [] };
let current = null, logCount = 0, wasRunning = false;

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
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("hidden", p.id !== b.dataset.tab));
  if (b.dataset.tab === "setup") loadSetup();
}));

// ---------------------------------------------------------------- state & settings
async function loadState() {
  S = await api("/api/state");
  const sel = $("#whisper_size");
  if (!sel.options.length) S.sizes.forEach((s) => sel.add(new Option(s, s)));
  const lang = $("#language");
  if (!lang.options.length) Object.entries(S.languages).forEach(([code, name]) => lang.add(new Option(name, code)));
  for (const k of ["whisper_size", "language", "provider", "base_url", "target_length", "render_mode"]) $("#" + k).value = S.settings[k] ?? "";
  renderClips();
  renderTimeline();
}

const saveSettings = (patch) => post("/api/settings", patch).then((s) => (S.settings = s)).catch(fail);
for (const k of ["whisper_size", "language", "base_url", "target_length", "render_mode", "model"]) {
  $("#" + k).addEventListener("change", (e) => {
    saveSettings({ [k]: e.target.value });
    if (k === "base_url") refreshModels();
    if (k === "target_length") renderTimeline();
  });
}
$("#provider").onchange = async (e) => {
  $("#base_url").value = DEFAULT_URLS[e.target.value];
  await saveSettings({ provider: e.target.value, base_url: $("#base_url").value });
  refreshModels();
};

async function refreshModels() {
  const sel = $("#model"), status = $("#llmStatus");
  status.textContent = "Connecting…";
  status.className = "muted small";
  try {
    const { models } = await api(`/api/llm/models?provider=${encodeURIComponent($("#provider").value)}&base_url=${encodeURIComponent($("#base_url").value)}`);
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

// ---------------------------------------------------------------- media library
function renderClips() {
  $("#clips").innerHTML = S.clips.map((c, i) => `
    <li data-id="${c.id}" class="${current === c.id ? "sel" : ""}">
      <span class="name" title="${esc(c.path)}">${i + 1}. ${esc(c.name)}</span>
      <span class="btns">
        <button data-act="up" aria-label="Move up">↑</button><button data-act="down" aria-label="Move down">↓</button>
        <button data-act="del" aria-label="Remove clip">✕</button>
      </span>
      <span class="muted small">${fmtT(c.duration)} · ${fmtSize(c.size)} · ${c.width}x${c.height}${c.segments ? " · transcribed" : ""}</span>
    </li>`).join("") || '<li class="muted">No clips yet</li>';
}

$("#clips").onclick = async (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  const act = e.target.dataset.act, id = li.dataset.id;
  try {
    if (act === "del") { if (!confirm("Remove this clip from the project?")) return; await api(`/api/clips/${id}`, { method: "DELETE" }); }
    else if (act) await post(`/api/clips/${id}/move`, { delta: act === "up" ? -1 : 1 });
    else return preview(id, 0);
    await loadState();
  } catch (err) { fail(err); }
};

function preview(id, t) {
  const c = S.clips.find((x) => x.id === id), v = $("#player");
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
  if (!window.pywebview?.api) return;
  e.preventDefault();
  try {
    for (const path of (await window.pywebview.api.pick_files()) || []) await post("/api/clips/path", { path });
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

// ---------------------------------------------------------------- timeline & transcript
function renderTimeline() {
  let kept = 0, speech = 0, total = 0;
  const tracks = [], text = [];
  S.clips.forEach((c, ci) => {
    total += c.duration;
    const segs = c.segments || [];
    tracks.push(`<div class="track"><div class="lbl">${ci + 1}. ${esc(c.name)}</div><div class="lane">${segs.map((s) =>
      `<i class="${s.keep ? "k" : ""}" style="left:${(s.start / c.duration) * 100}%;width:${Math.max(0.15, ((s.end - s.start) / c.duration) * 100)}%"></i>`).join("")}</div></div>`);
    if (!segs.length) return;
    text.push(`<h3>${ci + 1}. ${esc(c.name)}</h3>`);
    segs.forEach((s, i) => {
      speech += s.end - s.start;
      if (s.keep) kept += s.end - s.start;
      text.push(`<div class="seg ${s.keep ? "k" : ""}" data-id="${c.id}" data-i="${i}">
        <input type="checkbox" ${s.keep ? "checked" : ""} aria-label="Keep segment">
        <span class="t">${fmtT(s.start)}</span>
        <span class="txt">${esc(s.text)}${s.score ? `<span class="why">★${s.score} ${esc(s.reason || "")}</span>` : ""}</span></div>`);
    });
  });
  $("#tracks").innerHTML = tracks.join("");
  $("#transcript").innerHTML = text.join("") || '<p class="muted">Run “Auto-edit with AI” to transcribe and get the AI’s cut.</p>';
  const target = S.settings.target_length ? ` · target ${esc(S.settings.target_length)}` : "";
  $("#summary").innerHTML = total ? `Kept <b>${fmtT(kept)}</b> of ${fmtT(speech)} speech (${fmtT(total)} raw)${target}` : "";
}

$("#transcript").addEventListener("click", async (e) => {
  const row = e.target.closest(".seg");
  if (!row) return;
  const c = S.clips.find((x) => x.id === row.dataset.id), seg = c.segments[+row.dataset.i];
  if (e.target.type === "checkbox") {
    try {
      Object.assign(seg, await api(`/api/clips/${c.id}/segments/${row.dataset.i}`, { method: "PATCH", body: JSON.stringify({ keep: e.target.checked }) }));
      renderTimeline();
    } catch (err) { e.target.checked = !e.target.checked; fail(err); }
  } else preview(c.id, seg.start);
});

// ---------------------------------------------------------------- jobs & console
function appendLog(line, isErr) {
  const log = $("#log"), el = document.createElement("div");
  el.textContent = line;
  if (isErr || line.includes("ERROR")) el.className = "err";
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

async function poll() {
  try {
    const j = await api(`/api/job?since=${logCount}`);
    if (j.log_count < logCount) { logCount = 0; $("#log").innerHTML = ""; return; } // a new job started
    j.logs.forEach((l) => appendLog(l));
    logCount = j.log_count;
    if (j.running || wasRunning) {
      $("#bar").style.width = j.progress * 100 + "%";
      $("#stepLabel").textContent = j.running ? j.step : j.error ? "Failed" : "Done";
    }
    $("#runBtn").disabled = $("#exportBtn").disabled = j.running;
    if (wasRunning && !j.running) {
      await loadState();
      if (j.result && j.result.video) {
        $("#result").innerHTML = `<p>✅ Saved to <code>${esc(j.result.folder)}</code><br>
          <button id="reveal">Open output folder</button>
          <a href="${j.result.video}" download>Download final_youtube_cut.mp4</a><a href="${j.result.edl}" download>Download .edl</a></p>`;
        $("#reveal").onclick = () => post("/api/reveal").catch(fail);
      }
      if (document.querySelector('.tab.active').dataset.tab === "setup") loadSetup();
    }
    wasRunning = j.running;
  } catch { /* server restarting — keep polling */ }
}

async function startJob(path) {
  try { logCount = 0; $("#log").innerHTML = ""; $("#result").innerHTML = ""; await post(path); wasRunning = true; poll(); }
  catch (e) { fail(e); }
}
$("#runBtn").onclick = () => startJob("/api/run");
$("#exportBtn").onclick = () => startJob("/api/export");

// ---------------------------------------------------------------- setup tab
async function loadSetup() {
  const s = await api("/api/setup");
  $("#ffmpegStatus").innerHTML = s.ffmpeg ? `<span class="ok">✓ Found</span> <code>${esc(s.ffmpeg)}</code>` : `<span class="err">✗ ${esc(s.ffmpeg_error)}</span>`;
  $("#whisperList").innerHTML = s.whisper_error ? `<li class="err">${esc(s.whisper_error)}</li>` :
    Object.entries(s.whisper).map(([size, ok]) => `<li><b style="width:70px">${size}</b>${ok ? '<span class="ok">✓ downloaded</span>' :
      `<button data-size="${size}">Download</button>`}</li>`).join("");
}
$("#whisperList").onclick = (e) => {
  if (!e.target.dataset.size) return;
  e.target.disabled = true;
  e.target.textContent = "Downloading… (see Editor console)";
  startJob(`/api/setup/whisper/${e.target.dataset.size}`);
};
$("#testLlm").onclick = async () => { $("#setupLlm").textContent = await refreshModels(); };

loadState().then(refreshModels).catch(fail);
setInterval(poll, 1000);
poll();
