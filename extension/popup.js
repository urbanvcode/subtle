const $ = (id) => document.getElementById(id);
const PLAY_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="13" rx="2"/><path d="M7 15h4M14 15h3M7 11h10"/></svg>';
const STOP_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';

const DEFAULTS = {
  language: "ja",
  target: "en",
  mode: "whisper",
  asrModel: "auto",
  llmModel: "",
  serverUrl: "ws://127.0.0.1:8765",
  fontScale: 1,
  position: 8,
  showPartial: true,
  showOriginal: false,
  saveTranscript: false,
  speakers: false,
  autoStart: false,
  ...SubtleStyle.DEFAULTS, // subtitle look (substyle.js)
};
const VERSION = chrome.runtime.getManifest().version;

const SPOKEN = [
  ["ja", "Japanese"], ["zh", "Chinese"], ["ko", "Korean"], ["en", "English"], ["es", "Spanish"],
  ["fr", "French"], ["de", "German"], ["it", "Italian"], ["pt", "Portuguese"], ["ru", "Russian"],
  ["th", "Thai"], ["vi", "Vietnamese"], ["id", "Indonesian"], ["hi", "Hindi"], ["ar", "Arabic"],
  ["tr", "Turkish"], ["ur", "Urdu"], ["auto", "Auto-detect"],
];
const TARGETS = [
  ["en", "English"], ["ja", "Japanese"], ["zh", "Chinese"], ["ko", "Korean"], ["es", "Spanish"],
  ["fr", "French"], ["de", "German"], ["it", "Italian"], ["pt", "Portuguese"], ["ru", "Russian"],
  ["ar", "Arabic"], ["hi", "Hindi"], ["ur", "Urdu"], ["id", "Indonesian"], ["vi", "Vietnamese"],
  ["th", "Thai"], ["tr", "Turkish"], ["nl", "Dutch"], ["pl", "Polish"],
];

let tab = null;
let s = { ...DEFAULTS };
let info = null; // model lists from the server

const save = (patch) => {
  Object.assign(s, patch);
  return chrome.storage.sync.set(patch).catch(() => {});
};

// chrome.storage.sync allows about two writes a second; dragging a slider fires far more often.
function throttle(fn, ms) {
  let timer = 0;
  let waiting;
  const fire = () => {
    timer = 0;
    if (waiting === undefined) return;
    const v = waiting;
    waiting = undefined;
    fn(v);
    timer = setTimeout(fire, ms);
  };
  return (v) => {
    waiting = v;
    if (!timer) fire();
  };
}

function fillSelect(sel, items, value) {
  sel.innerHTML = "";
  for (const [v, label] of items) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    sel.appendChild(o);
  }
  if (value !== undefined) sel.value = value;
}

const shortName = (id) => id.split("/").pop();

// ---- status / start-stop -------------------------------------------------------
async function refreshStatus() {
  const state = await chrome.runtime.sendMessage({ cmd: "status" });
  const here = state.tabId !== null && state.tabId === tab.id;
  const elsewhere = state.tabId !== null && !here;
  $("status").className = "status " + (here ? state.status : "");
  const auto = here && state.source === "element" ? "Auto (in-page audio) · " : "";
  $("statusText").textContent = here
    ? auto + (state.detail || { live: "Live", connecting: "Connecting…", error: "Error" }[state.status] || state.status)
    : elsewhere
      ? "Running on another tab"
      : "Not running";
  const btn = $("toggle");
  btn.classList.toggle("stop", here);
  btn.innerHTML = here ? `${STOP_ICON}Stop subtitles` : `${PLAY_ICON}Start subtitles on this tab`;
}

async function toggle() {
  const state = await chrome.runtime.sendMessage({ cmd: "status" });
  if (state.tabId === tab.id) {
    await chrome.runtime.sendMessage({ cmd: "stop" });
  } else {
    try {
      // Must be called from the popup (a user gesture) for tab capture to be allowed.
      const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
      await chrome.runtime.sendMessage({ cmd: "start", tabId: tab.id, streamId });
    } catch (e) {
      $("status").className = "status error";
      $("statusText").textContent = String(e.message || e);
      return;
    }
  }
  refreshStatus();
}

// ---- Catch me up / Save subtitles --------------------------------------------------
function toolHint(text, err = false) {
  const el = $("toolHint");
  el.textContent = text;
  el.className = "hint" + (err ? " err" : "") + (text ? "" : " hidden");
}

const pad = (n, w = 2) => String(n).padStart(w, "0");
function stamp(sec, sep) {
  const ms = Math.round(sec * 1000);
  return `${pad(Math.floor(ms / 3600000))}:${pad(Math.floor(ms / 60000) % 60)}:${pad(Math.floor(ms / 1000) % 60)}${sep}${pad(ms % 1000, 3)}`;
}

// Lines in time order, each kept on screen until shortly before the next one (2-7 s, by length).
function tidyCues(cues) {
  const list = [...cues].sort((a, b) => a.start - b.start);
  return list.map((c, i) => {
    const next = list[i + 1];
    const want = Math.min(7, Math.max(2, c.text.length * 0.07));
    let end = Math.max(c.end + 0.6, c.start + want);
    if (next) end = Math.min(end, Math.max(c.start + 0.5, next.start - 0.05));
    return { ...c, end };
  });
}

function buildSubtitleFile(cues, fmt, withOriginal) {
  const lines = tidyCues(cues).map((c, i) => {
    const text = withOriginal && c.src && c.src !== c.text ? `${c.text}\n${c.src}` : c.text;
    const sep = fmt === "vtt" ? "." : ",";
    const head = fmt === "vtt" ? "" : `${i + 1}\n`;
    return `${head}${stamp(c.start, sep)} --> ${stamp(c.end, sep)}\n${text}\n`;
  });
  return (fmt === "vtt" ? "WEBVTT\n\n" : "") + lines.join("\n");
}

async function saveSubtitles(fmt) {
  let res = null;
  try {
    res = await chrome.tabs.sendMessage(tab.id, { type: "get-cues" });
  } catch {}
  if (!res || !res.cues || !res.cues.length) {
    return toolHint("No subtitles on this page yet. Start subtitles and play the video first.", true);
  }
  const body = buildSubtitleFile(res.cues, fmt, s.showOriginal);
  const name = (res.title || "subtitles").replace(/[\\/:*?"<>|]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) || "subtitles";
  const url = URL.createObjectURL(new Blob(["﻿" + body], { type: fmt === "vtt" ? "text/vtt" : "application/x-subrip" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${name}.${fmt}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  toolHint(`Saved ${res.cues.length} lines as ${name}.${fmt}` + (s.showOriginal ? " (with the original lines)" : ""));
}

// ---- model lists from the server -------------------------------------------------
function fetchInfo() {
  return new Promise((resolve) => {
    let ws;
    const done = (v) => {
      try {
        ws && ws.close();
      } catch {}
      resolve(v);
    };
    const timer = setTimeout(() => done(null), 4000);
    try {
      ws = new WebSocket(s.serverUrl);
    } catch {
      clearTimeout(timer);
      return resolve(null);
    }
    ws.onopen = () => ws.send(JSON.stringify({ type: "info" }));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "info") {
        clearTimeout(timer);
        done(msg);
      }
    };
    ws.onerror = () => {
      clearTimeout(timer);
      done(null);
    };
  });
}

// What "Auto" picks, mirroring the app's rule (server/engines.py default_asr_model): language-specific
// models only for the Best engine (they transcribe; the translator does the rest), Whisper otherwise.
function autoChoice(llm, language, asr) {
  const has = (re) => asr.some((m) => re.test(m));
  if (llm && language === "ja" && has(/parakeet/i)) return "Parakeet for Japanese";
  if (llm && (language === "zh" || language === "yue") && has(/sensevoice/i)) return "SenseVoice for Chinese";
  return llm ? "turbo for speed" : "large-v3";
}


function renderModels() {
  const llm = s.mode === "llm" || s.target !== "en";

  // Speech model
  const asr = info ? info.asr_models : s.asrModel !== "auto" ? [s.asrModel] : [];
  const items = [["auto", `Auto (${autoChoice(llm, s.language, asr)})`]];
  for (const m of asr) {
    let note = "";
    if (/parakeet/i.test(m)) note = " - Japanese, Best engine";
    else if (/sensevoice/i.test(m)) note = " - Chinese, Best engine";
    else if (/kotoba|anime-whisper/i.test(m)) note = " - Japanese";
    else if (/turbo/.test(m) && !llm) note = " - can't translate well";
    else if (/base|tiny|small/.test(m)) note = " - low accuracy";
    items.push([m, shortName(m) + note]);
  }
  fillSelect($("asrModel"), items, asr.includes(s.asrModel) ? s.asrModel : "auto");

  // LLM model
  const llms = info ? info.llm_models : s.llmModel ? [s.llmModel] : [];
  const llmItems = llms.length ? llms.map((m) => [m, shortName(m)]) : [["", info ? "No translator installed" : "Start Subtle to list"]];
  fillSelect($("llmModel"), llmItems);
  let pick = llms.includes(s.llmModel) ? s.llmModel : "";
  if (!pick && llms.length) {
    // The control panel's default, else a small, fast instruct model for live subtitles.
    pick = (info && llms.includes(info.llm_default) && info.llm_default) ||
      llms.find((m) => /qwen3-4b|qwen3-8b|instruct/i.test(m)) || llms[0];
    save({ llmModel: pick });
  }
  $("llmModel").value = pick;

  const hint = $("llmHint");
  if (info && info.llm_error) {
    hint.textContent = `${info.llm_error}.`;
    hint.className = "hint err";
  } else if (!info) {
    hint.textContent = "Subtle isn't running - start it from the desktop or Start-menu shortcut.";
    hint.className = "hint err";
  } else {
    hint.className = "hint hidden";
  }
}

function renderMode() {
  const llm = s.mode === "llm" || s.target !== "en";
  $("modeWhisper").classList.toggle("on", !llm);
  $("modeLlm").classList.toggle("on", llm);
  $("modeWhisper").setAttribute("aria-checked", String(!llm));
  $("modeLlm").setAttribute("aria-checked", String(llm));
  $("modeWhisper").disabled = s.target !== "en";
  $("llmRow").classList.toggle("hidden", !llm);
  if (!llm) $("llmHint").classList.add("hidden");
  $("modeHint").textContent =
    s.target !== "en"
      ? "Subtitles in languages other than English always use the translator."
      : llm
        ? "Whisper writes down the speech, the translator turns it into natural lines using context. ~0.1-0.2 s per line."
        : "Whisper translates straight to English. Fastest, more literal.";
  renderModels();
}

function renderVersion() {
  const v = $("version");
  if (!info) {
    v.textContent = `Subtle v${VERSION} · app not running`;
    v.className = "hint";
  } else if (info.version !== VERSION) {
    v.textContent = `Extension v${VERSION} · app v${info.version || "?"} - versions differ: quit and restart Subtle from the tray (and reload the extension if it's older)`;
    v.className = "hint err";
  } else if (info.update && info.update.version) {
    v.textContent = `Subtle v${VERSION} · version ${info.update.version} is available on GitHub (see the Subtle app)`;
    v.className = "hint";
  } else {
    v.textContent = `Subtle v${VERSION}`;
    v.className = "hint";
  }
  if (info && info.transcript_dir) $("transcriptHint").textContent = `Saved as text files in ${info.transcript_dir}. Off: nothing is written to disk.`;
}

async function loadInfo() {
  $("refresh").disabled = true;
  info = await fetchInfo();
  $("refresh").disabled = false;
  renderMode();
  renderVersion();
}

// ---- subtitle style ----------------------------------------------------------------
const ST = SubtleStyle;
let previewBox = null;

function renderPreview() {
  if (!previewBox) {
    // Same stylesheet as the overlay on the video, in its own shadow root so the popup's CSS can't leak in.
    const host = document.createElement("div");
    host.style.cssText = "width: 100%; display: flex; justify-content: center;";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${ST.CSS}</style><div class="box">
        <div class="line prev">Where were you last night?</div>
        <div class="line cur">I was at the station, waiting for the last train.</div>
        <div class="line src">駅で終電を待ってた。</div></div>`;
    previewBox = root.querySelector(".box");
    $("preview").appendChild(host);
  }
  const size = Number($("size").value) || s.fontScale || 1;
  previewBox.style.fontSize = `${Math.max(10, Math.min(22, 13 * size))}px`;
  previewBox.style.width = `${Math.max(40, Math.min(100, s.lineWidth))}%`;
  previewBox.querySelector(".prev").style.display = s.keepPrev ? "" : "none";
  previewBox.querySelector(".src").style.display = $("original").checked ? "" : "none";
  ST.apply(previewBox, s);
  // highlight the preset that matches, if any
  for (const b of document.querySelectorAll("#presets button")) {
    const p = ST.PRESETS[b.dataset.preset];
    const match = Object.entries(p).every(([k, v]) => k === "label" || s[k] === v);
    b.classList.toggle("on", match);
  }
  for (const b of document.querySelectorAll("#textColors .swatch")) b.classList.toggle("on", b.dataset.color === s.textColor);
  for (const b of document.querySelectorAll("#bgColors .swatch")) b.classList.toggle("on", b.dataset.color === s.bgColor);
}

function setStyle(patch) {
  save(patch);
  syncStyleControls();
  renderPreview();
}

function syncStyleControls() {
  $("bgOpacity").value = s.bgOpacity;
  $("bgOpacityVal").textContent = s.bgOpacity > 0 ? `${s.bgOpacity}%` : "No box";
  $("edge").value = s.edge;
  $("font").value = s.font;
  $("weight").value = s.weight;
  $("lineWidth").value = s.lineWidth;
  $("lineWidthVal").textContent = `${s.lineWidth}%`;
  $("srcSize").value = s.srcSize;
  $("srcSizeVal").textContent = `${s.srcSize}%`;
  $("lineTime").value = s.lineTime;
  $("keepPrev").checked = s.keepPrev;
}

function initStyle() {
  const opts = (obj) => Object.entries(obj).map(([v, [label]]) => [v, label]);
  fillSelect($("edge"), opts(ST.EDGES));
  fillSelect($("font"), opts(ST.FONTS));
  fillSelect($("weight"), opts(ST.WEIGHTS));
  fillSelect($("lineTime"), opts(ST.TIMES));
  for (const [key, p] of Object.entries(ST.PRESETS)) {
    const b = document.createElement("button");
    b.textContent = p.label;
    b.title = p.label;
    b.dataset.preset = key;
    b.onclick = () => {
      const { label, ...patch } = p;
      setStyle(patch);
    };
    $("presets").appendChild(b);
  }
  const swatches = (el, colors, key) => {
    for (const [color, name] of colors) {
      const b = document.createElement("button");
      b.className = "swatch";
      b.title = name;
      b.dataset.color = color;
      b.style.background = color;
      b.onclick = () => setStyle({ [key]: color, ...(key === "bgColor" && !s.bgOpacity ? { bgOpacity: 62 } : {}) });
      el.appendChild(b);
    }
  };
  swatches($("textColors"), ST.TEXT_COLORS, "textColor");
  swatches($("bgColors"), ST.BG_COLORS, "bgColor");

  // Sliders preview live while dragging; storage (two writes a second at most) catches up.
  const slider = (id, key, fmt) => {
    const store = throttle((v) => save({ [key]: v }), 500);
    $(id).oninput = (e) => {
      const v = Number(e.target.value);
      s[key] = v;
      $(id + "Val").textContent = fmt(v);
      renderPreview();
      store(v);
    };
  };
  slider("bgOpacity", "bgOpacity", (v) => (v > 0 ? `${v}%` : "No box"));
  slider("lineWidth", "lineWidth", (v) => `${v}%`);
  slider("srcSize", "srcSize", (v) => `${v}%`);
  for (const [id, key] of [["edge", "edge"], ["font", "font"], ["weight", "weight"], ["lineTime", "lineTime"]]) {
    $(id).onchange = (e) => setStyle({ [key]: e.target.value });
  }
  $("keepPrev").onchange = (e) => setStyle({ keepPrev: e.target.checked });
  $("styleReset").onclick = () => setStyle({ ...ST.DEFAULTS });
  syncStyleControls();
  renderPreview();
}

// ---- init ------------------------------------------------------------------------
async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  s = { ...DEFAULTS, ...(await chrome.storage.sync.get(DEFAULTS)) };

  fillSelect($("language"), SPOKEN, s.language);
  fillSelect($("target"), TARGETS, s.target);
  $("size").value = s.fontScale;
  $("sizeVal").textContent = `${Math.round(s.fontScale * 100)}%`;
  $("pos").value = s.position;
  $("posVal").textContent = `${s.position}%`;
  $("partial").checked = s.showPartial;
  $("original").checked = s.showOriginal;
  $("transcript").checked = s.saveTranscript;
  $("speakers").checked = s.speakers;
  $("autoStart").checked = s.autoStart;
  $("server").value = s.serverUrl;

  $("language").onchange = (e) => {
    save({ language: e.target.value });
    renderModels();            // "Auto" may pick a different model for this language
  };
  $("target").onchange = (e) => {
    save({ target: e.target.value });
    renderMode();
  };
  $("modeWhisper").onclick = () => {
    save({ mode: "whisper" });
    renderMode();
  };
  $("modeLlm").onclick = () => {
    save({ mode: "llm" });
    renderMode();
  };
  $("asrModel").onchange = (e) => save({ asrModel: e.target.value });
  $("llmModel").onchange = (e) => save({ llmModel: e.target.value });
  const saveSize = throttle((v) => save({ fontScale: v }), 500);
  const savePos = throttle((v) => save({ position: v }), 500);
  $("size").oninput = (e) => {
    $("sizeVal").textContent = `${Math.round(e.target.value * 100)}%`;
    saveSize(Number(e.target.value));
  };
  $("pos").oninput = (e) => {
    $("posVal").textContent = `${e.target.value}%`;
    savePos(Number(e.target.value));
  };
  $("partial").onchange = (e) => save({ showPartial: e.target.checked });
  $("original").onchange = (e) => save({ showOriginal: e.target.checked });
  $("transcript").onchange = (e) => save({ saveTranscript: e.target.checked });
  $("speakers").onchange = (e) => save({ speakers: e.target.checked });
  $("autoStart").onchange = (e) => save({ autoStart: e.target.checked });
  $("server").onchange = (e) => {
    save({ serverUrl: e.target.value.trim() });
    loadInfo();
  };
  $("refresh").onclick = loadInfo;
  $("openPanel").onclick = () => {
    // Ask the Subtle app to show its window (it's a normal Windows window, not a web page).
    try {
      const ws = new WebSocket(s.serverUrl);
      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "show_panel" }));
        setTimeout(() => ws.close(), 300);
      };
      ws.onerror = () => {
        $("llmHint").textContent = "Subtle isn't running - start it from the desktop or Start-menu shortcut.";
        $("llmHint").className = "hint err";
      };
    } catch {}
  };
  $("toggle").onclick = toggle;
  $("catchUp").onclick = async () => {
    await chrome.runtime.sendMessage({ cmd: "catch-up", tabId: tab.id }).catch(() => {});
    window.close(); // the summary appears on the video
  };
  $("saveSubs").onclick = () => $("saveRow").classList.toggle("hidden");
  for (const b of document.querySelectorAll(".fmt")) b.onclick = () => saveSubtitles(b.dataset.fmt);
  initStyle();
  $("size").addEventListener("input", renderPreview);
  $("original").addEventListener("change", renderPreview);

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.cmd === "capture-status") setTimeout(refreshStatus, 50);
  });
  renderMode();
  renderVersion();
  refreshStatus();
  loadInfo();
}

init();
