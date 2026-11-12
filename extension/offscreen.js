// Offscreen document: captures the tab's audio, keeps it audible, resamples it to
// 16 kHz mono and streams it to the local GPU server. In auto-start mode the audio comes
// from the page's own <video> instead (sent here by the content script over a port). Subtitles come back here and
// are forwarded to the service worker, which sends them to the tab.

let stream = null;
let playCtx = null;     // plays the tab audio back to the speakers (capture mutes the tab)
let capCtx = null;      // 16 kHz context feeding the worklet
let ws = null;
let serverUrl = "";
let config = {};
let running = false;
let paused = false;
let reconnectTimer = null;

function status(s, detail = "") {
  chrome.runtime.sendMessage({ cmd: "capture-status", status: s, detail }).catch(() => {});
}

function connect() {
  if (!running) return;
  try {
    ws = new WebSocket(serverUrl);
  } catch (e) {
    status("error", `Bad server URL: ${serverUrl}`);
    return;
  }
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    status("live", "Connected to GPU server");
    ws.send(JSON.stringify({ type: "config", ...config }));
    if (paused) ws.send(JSON.stringify({ type: "pause" }));
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.type === "sub" || msg.type === "clear") {
      chrome.runtime.sendMessage({ cmd: msg.type, data: msg }).catch(() => {});
    } else if (msg.type === "status") {
      // Model loading, LLM problems, etc.
      const s = msg.level === "error" ? "error" : msg.level === "busy" ? "connecting" : "live";
      status(s, msg.level === "ok" && msg.message === "Ready" ? "Connected to GPU server" : msg.message);
    }
  };
  ws.onclose = () => {
    ws = null;
    if (!running) return;
    status("error", "Subtle isn't running - start it from the desktop shortcut. Retrying…");
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => {};
}

function sendAudio(buf) {
  // 64 kB is one second of audio. If the server stalls (e.g. loading a model) keep a few seconds
  // queued and drop the rest: subtitles for audio much older than that would only arrive late.
  if (!paused && ws && ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 1 << 18) ws.send(buf);
}

async function start(streamId, url, cfg, source) {
  await stop();
  running = true;
  paused = false;
  serverUrl = url;
  config = cfg || {};

  if (source === "element") {
    // Audio arrives from the content script (see onConnect below); only the server link is needed.
    status("connecting", "Auto: connecting to GPU server…");
    connect();
    return;
  }

  stream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    video: false,
  });
  stream.getAudioTracks()[0].addEventListener("ended", () => status("ended"));

  playCtx = new AudioContext();
  playCtx.createMediaStreamSource(stream).connect(playCtx.destination);

  capCtx = new AudioContext({ sampleRate: 16000 });
  await capCtx.audioWorklet.addModule(chrome.runtime.getURL("capture-worklet.js"));
  const node = new AudioWorkletNode(capCtx, "capture", { numberOfInputs: 1, numberOfOutputs: 0 });
  node.port.onmessage = (ev) => sendAudio(ev.data);
  capCtx.createMediaStreamSource(stream).connect(node);

  status("connecting", "Connecting to GPU server…");
  connect();
}

async function stop() {
  running = false;
  clearTimeout(reconnectTimer);
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  for (const ctx of [playCtx, capCtx]) if (ctx) await ctx.close().catch(() => {});
  playCtx = capCtx = null;
}

function control(type) {
  if (type === "pause") paused = true;
  if (type === "play") paused = false;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type }));
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.target !== "offscreen") return;
  (async () => {
    try {
      if (msg.cmd === "start") await start(msg.streamId, msg.serverUrl, msg.config, msg.source);
      else if (msg.cmd === "stop") await stop();
      else if (msg.cmd === "control") control(msg.type);
      else if (msg.cmd === "config") {
        config = msg.config || config;
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "config", ...config }));
      }
      sendResponse({ ok: true });
    } catch (e) {
      status("error", String(e.message || e));
      sendResponse({ ok: false, error: String(e.message || e) });
    }
  })();
  return true;
});

// Auto-start: 32 ms chunks of the page video's audio (base64 float32) from the content script.
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "subtle-audio") return;
  chrome.runtime.sendMessage({ cmd: "element-live", live: true }).catch(() => {});
  port.onMessage.addListener((b64) => {
    if (!running || typeof b64 !== "string") return;
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    sendAudio(bytes.buffer);
  });
  port.onDisconnect.addListener(() => {
    chrome.runtime.sendMessage({ cmd: "element-live", live: false }).catch(() => {});
  });
});
