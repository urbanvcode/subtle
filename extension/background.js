// Service worker: owns which tab is being captured and routes messages between
// the popup, the offscreen capture document and the content scripts.

const OFFSCREEN_URL = "offscreen.html";
// Firefox has no offscreen documents and no tab capture: offscreen.js runs in this background page,
// and subtitles always come from the page's own video (in-page capture).
const FIREFOX = !chrome.offscreen;

function toOffscreen(msg) {
  const m = { target: "offscreen", ...msg };
  if (globalThis.__subtleOffscreen) return globalThis.__subtleOffscreen(m);
  return chrome.runtime.sendMessage(m);
}
const DEFAULTS = {
  serverUrl: "ws://127.0.0.1:8765",
  language: "ja",
  target: "en",
  mode: "whisper",
  asrModel: "auto",
  llmModel: "",
  saveTranscript: false,
  speakers: false,
  autoStart: false,
  learning: false,
  reading: "furigana",
};
// (the translator's address is set in the Subtle window, not here)
const CONFIG_KEYS = ["language", "target", "mode", "asrModel", "llmModel", "saveTranscript", "speakers", "learning", "reading"];

async function serverConfig(pageTitle) {
  const s = await chrome.storage.sync.get(DEFAULTS);
  const cfg = {
    language: s.language,
    target: s.target,
    mode: s.mode,
    asr_model: s.asrModel,
    llm_model: s.llmModel,
    save_transcript: !!s.saveTranscript,
    speakers: !!s.speakers,
    learning: !!s.learning,
    reading: s.reading || "furigana",
  };
  if (pageTitle) cfg.page_title = pageTitle; // heading of a saved transcript only
  return cfg;
}

// The state lives in chrome.storage.session (it survives the worker being shut down) with a copy
// in memory, so routing a subtitle doesn't cost a storage read and two updates can't overwrite
// each other.
let stateCache = null;

async function getState() {
  if (!stateCache) {
    const { state } = await chrome.storage.session.get("state");
    if (!stateCache) stateCache = state || { tabId: null, status: "idle", detail: "" };
  }
  return stateCache;
}

async function setState(patch) {
  const state = (stateCache = { ...(await getState()), ...patch });
  await chrome.storage.session.set({ state });
  const on = state.tabId !== null;
  const color = state.status === "error" ? "#d33" : state.status === "live" ? "#2a7" : "#888";
  const text = on ? (state.status === "error" ? "!" : state.source === "element" ? "AUTO" : "ON") : "";
  await chrome.action.setBadgeText({ text });
  await chrome.action.setBadgeBackgroundColor({ color });
  if (on) {
    // a tab-specific badge (e.g. the "click to subtitle" hint) would otherwise hide the global one
    await chrome.action.setBadgeText({ tabId: state.tabId, text }).catch(() => {});
    await chrome.action.setBadgeBackgroundColor({ tabId: state.tabId, color }).catch(() => {});
    await chrome.action.setTitle({ tabId: state.tabId, title: "Subtle" }).catch(() => {});
  }
  return state;
}

// ---- auto-start ---------------------------------------------------------------------------
const needsClick = new Set(); // tabs where in-page audio can't be read: they show the hint badge
const userStopped = new Set(); // tabs where the user stopped subtitles: no auto-start until reload

async function hintClick(tabId, reason) {
  needsClick.add(tabId);
  const why = reason === "drm" ? "protected (DRM) video" : "this player's audio can't be read directly";
  if (FIREFOX) {
    // no tab capture in Firefox: there is nothing to click
    await chrome.action.setTitle({ tabId, title: `Subtle can't subtitle this video in Firefox - ${why}` }).catch(() => {});
    return;
  }
  await chrome.action.setBadgeText({ tabId, text: "▶" }).catch(() => {});
  await chrome.action.setBadgeBackgroundColor({ tabId, color: "#e0a400" }).catch(() => {});
  await chrome.action
    .setTitle({ tabId, title: `Subtle: click here (or Alt+Shift+S) to subtitle this video - ${why}` })
    .catch(() => {});
}

async function startElement(tabId, frameId) {
  const current = await getState();
  if (current.tabId !== null) await stop(false);
  const { serverUrl } = await chrome.storage.sync.get(DEFAULTS);
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const config = await serverConfig(tab && tab.title);
  await ensureOffscreen();
  await setState({ tabId, status: "connecting", detail: "", subFrame: frameId, source: "element", elementLive: false });
  await toOffscreen({ cmd: "start", serverUrl, config, source: "element" });
  toTab(tabId, { type: "active", active: true });
  if (frameId === null || frameId === undefined) {
    // Started by hand (Firefox): whichever frame has the video takes it.
    await injectContent(tabId);
    toTab(tabId, { type: "active", active: true });
    toTab(tabId, { type: "element-start", any: true });
    setTimeout(async () => {
      const st = await getState();
      if (st.tabId === tabId && st.source === "element" && !st.elementLive) {
        await setState({ status: "error", detail: "No video with sound found on this page. Play the video, then try again." });
      }
    }, 6000);
  } else {
    toTab(tabId, { type: "element-start" }, frameId);
  }
}

async function ensureOffscreen() {
  if (FIREFOX) return;
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ["USER_MEDIA"],
    justification: "Capture this tab's audio to generate live subtitles locally.",
  });
}

async function closeOffscreen() {
  if (FIREFOX) return;
  const contexts = await chrome.runtime.getContexts({ contextTypes: ["OFFSCREEN_DOCUMENT"] });
  if (contexts.length) await chrome.offscreen.closeDocument();
}

function toTab(tabId, msg, frameId) {
  if (tabId === null) return;
  const opts = frameId === undefined || frameId === null ? undefined : { frameId };
  chrome.tabs.sendMessage(tabId, msg, opts).catch(() => {});
}

// Page fullscreen in a captured tab only fills the window, so fullscreen the window as well,
// and put it back the way it was when the page leaves fullscreen.
async function syncWindowFullscreen(windowId, wantFullscreen) {
  try {
    const win = await chrome.windows.get(windowId);
    const { fsRestore } = await chrome.storage.session.get("fsRestore");
    if (wantFullscreen && win.state !== "fullscreen") {
      await chrome.storage.session.set({ fsRestore: { windowId, state: win.state } });
      await chrome.windows.update(windowId, { state: "fullscreen" });
    } else if (!wantFullscreen && fsRestore && fsRestore.windowId === windowId) {
      await chrome.storage.session.remove("fsRestore");
      if (win.state === "fullscreen") {
        const state = fsRestore.state === "fullscreen" ? "maximized" : fsRestore.state;
        await chrome.windows.update(windowId, { state });
      }
    }
  } catch {}
}

// Tabs opened before the extension was (re)loaded have no content script yet.
async function injectContent(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["substyle.js", "content.js"] });
  } catch {
    // Pages the extension can't touch (brave://, web store) - audio capture still works.
  }
}

async function start(tabId, streamId) {
  const current = await getState();
  if (current.tabId !== null) await stop(false);
  const { serverUrl } = await chrome.storage.sync.get(DEFAULTS);
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const config = await serverConfig(tab && tab.title);
  await ensureOffscreen();
  needsClick.delete(tabId);
  userStopped.delete(tabId);
  await setState({ tabId, status: "connecting", detail: "", subFrame: null, source: "tab" });
  await toOffscreen({ cmd: "start", streamId, serverUrl, config });
  await injectContent(tabId);
  toTab(tabId, { type: "active", active: true });
  toTab(tabId, { type: "query" }); // content script reports whether the video is playing
}

async function stop(byUser = true) {
  const { tabId, source } = await getState();
  if (source === "element") toTab(tabId, { type: "element-stop" });
  try {
    await toOffscreen({ cmd: "stop" });
  } catch {}
  await closeOffscreen();
  toTab(tabId, { type: "active", active: false });
  if (tabId !== null) {
    if (byUser) userStopped.add(tabId); // don't auto-start again right after the user stopped it
    await chrome.action.setBadgeText({ tabId, text: needsClick.has(tabId) ? "▶" : "" }).catch(() => {});
  }
  await setState({ tabId: null, status: "idle", detail: "", source: null });
  if (reloadPending) {
    const v = reloadPending;
    reloadPending = false;
    await maybeReload(v);
  }
}

const ANSWERED = new Set(["start", "stop", "status", "am-i-active"]); // commands that get a response

function handleMessage(msg, sender, sendResponse) {
  if (msg.target === "offscreen") return; // meant for the offscreen document

  (async () => {
    const state = await getState();
    switch (msg.cmd) {
      case "start":
        if (FIREFOX || !msg.streamId) await startElement(msg.tabId, null);
        else await start(msg.tabId, msg.streamId);
        sendResponse({ ok: true });
        break;
      case "stop":
        await stop();
        sendResponse({ ok: true });
        break;
      case "status":
        sendResponse(state);
        break;

      // From the offscreen document
      case "capture-status":
        await setState({ status: msg.status, detail: msg.detail || "" });
        if (msg.status === "ended") await stop(false);
        break;
      case "sub": {
        // Only the frame whose video is playing draws subtitles (top frame if none reported).
        if (state.tabId === null) break;
        const frameId = state.subFrame ?? 0;
        const sub = { type: "sub", ...msg.data };
        chrome.tabs.sendMessage(state.tabId, sub, { frameId }).catch((e) => {
          // That frame is gone (the page navigated): fall back to the top frame until a video reports in.
          if (frameId !== 0 && /Receiving end does not exist|No frame with id/i.test(String(e && e.message))) {
            setState({ subFrame: null });
            toTab(state.tabId, sub, 0);
          }
        });
        break;
      }
      case "clear":
        toTab(state.tabId, { type: "clear" });
        break;
      case "summary": // from the offscreen document: the app's answer to "Catch me up"
        toTab(state.tabId, { type: "summary", ...msg.data }, state.subFrame ?? 0);
        break;
      case "define-word": // learning mode: a word was clicked on the video
        if (sender.tab && sender.tab.id === state.tabId) {
          toOffscreen({ cmd: "define", word: msg.word, surface: msg.surface, reading: msg.reading, context: msg.context }).catch(() => {});
        }
        break;
      case "define": // from the offscreen document: the app's answer
        toTab(state.tabId, { type: "define", ...msg.data, word: (msg.data && msg.data.word) || "" }, state.subFrame ?? 0);
        break;
      case "app-version": // the Subtle app is newer than this extension: load the new files from disk
        await maybeReload(msg.version);
        break;
      case "catch-up": // the popup's button
        await catchUp(msg.tabId);
        break;

      // From content scripts: video play / pause / seek in the captured tab
      case "video":
        if (sender.tab && sender.tab.id === state.tabId) {
          const known = state.subFrame !== null && state.subFrame !== undefined;
          if (msg.event === "play") {
            if (sender.frameId !== state.subFrame) {
              if (known) toTab(state.tabId, { type: "clear" }, state.subFrame);
              await setState({ subFrame: sender.frameId });
            }
          } else if (known && sender.frameId !== state.subFrame) {
            break; // pause / seek of a video in another frame (an ad, a preview), not the one being subtitled
          }
          toOffscreen({ cmd: "control", type: msg.event }).catch(() => {});
        }
        break;
      case "fullscreen":
        // Only tab capture limits fullscreen to the window; in-page capture needs no help.
        if (sender.tab && sender.tab.id === state.tabId && state.source === "tab") {
          await syncWindowFullscreen(sender.tab.windowId, msg.on);
        }
        break;

      // Auto-start: a video started playing with sound in some tab
      case "auto-play": {
        const tabId = sender.tab && sender.tab.id;
        if (tabId === undefined || tabId === null) break;
        const { autoStart } = await chrome.storage.sync.get(DEFAULTS);
        if (!autoStart || userStopped.has(tabId)) break;
        if (state.tabId === tabId) {
          // already subtitling this tab; after navigation or in another frame, restart the in-page capture there
          if (state.source === "element" && (sender.frameId !== state.subFrame || !state.elementLive)) {
            await setState({ subFrame: sender.frameId });
            toTab(tabId, { type: "element-start" }, sender.frameId);
          }
          break;
        }
        if (msg.drm || needsClick.has(tabId)) {
          await hintClick(tabId, msg.drm ? "drm" : "blocked");
          break;
        }
        await startElement(tabId, sender.frameId); // subtitles follow you to the tab that's playing
        break;
      }
      case "element-failed":
        if (sender.tab && sender.tab.id === state.tabId && state.source === "element") {
          await stop(false);
          await hintClick(sender.tab.id, msg.reason);
        }
        break;
      case "element-live":
        if (state.source === "element") await setState({ elementLive: !!msg.live });
        break;
      case "am-i-active":
        sendResponse({ active: !!sender.tab && sender.tab.id === state.tabId });
        break;
    }
  })();
  return ANSWERED.has(msg.cmd); // keep sendResponse alive for the async handler, only when it will answer
}
chrome.runtime.onMessage.addListener(handleMessage);
globalThis.__subtleBackground = (msg) => {
  handleMessage(msg, {}, () => {});
};

// The extension is loaded from the Subtle folder, so when the app was updated, the new extension files
// are already on disk: reload them. Not while subtitles are running (that waits until they stop), and only
// once per app version (if the files on disk are still older, reloading again would not help).
let reloadPending = false;
async function maybeReload(appVersion) {
  const mine = chrome.runtime.getManifest().version;
  const num = (v) => String(v || "0").split(".").map((n) => parseInt(n, 10) || 0);
  const a = num(appVersion), b = num(mine);
  let newerApp = false;
  for (let i = 0; i < 3; i++) if ((a[i] || 0) !== (b[i] || 0)) { newerApp = (a[i] || 0) > (b[i] || 0); break; }
  if (!newerApp) return;
  const { reloadedFor } = await chrome.storage.local.get("reloadedFor");
  if (reloadedFor === appVersion) return;
  if ((await getState()).tabId !== null) {
    reloadPending = appVersion;
    return;
  }
  await chrome.storage.local.set({ reloadedFor: appVersion });
  chrome.runtime.reload();
}

// "Catch me up": the Subtle app summarises the last few minutes; the answer is shown on the video.
async function catchUp(tabId) {
  const state = await getState();
  if (state.tabId === null) {
    if (tabId !== undefined && tabId !== null) {
      await injectContent(tabId);
      toTab(tabId, { type: "summary", error: "Start subtitles first: Subtle summarises what it has heard." });
    }
    return;
  }
  toTab(state.tabId, { type: "summary", pending: true }, state.subFrame ?? 0);
  toOffscreen({ cmd: "summary", minutes: 5 }).catch(() => {});
}

// Keyboard shortcuts. Alt+Shift+S counts as a user gesture, so capture can start from here.
chrome.commands.onCommand.addListener(async (command, tab) => {
  if (!tab) return;
  if (command === "catch-up") return catchUp(tab.id);
  if (command === "hide-subs" || command === "repeat-line") {
    const state = await getState();
    const target = state.tabId ?? tab.id;
    toTab(target, { type: "hotkey", action: command === "hide-subs" ? "hide" : "repeat" },
      state.tabId !== null ? state.subFrame ?? 0 : 0);
    return;
  }
  if (command !== "toggle-subs") return;
  const state = await getState();
  if (state.tabId === tab.id) return stop();
  if (FIREFOX) return startElement(tab.id, null);
  try {
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    await start(tab.id, streamId);
  } catch (e) {
    await setState({ status: "error", detail: String(e.message || e) });
  }
});

// Translation settings changed in the popup: apply them to a running capture immediately.
chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area !== "sync" || !CONFIG_KEYS.some((k) => k in changes)) return;
  if ((await getState()).tabId === null) return;
  toOffscreen({ cmd: "config", config: await serverConfig() }).catch(() => {});
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  needsClick.delete(tabId);
  userStopped.delete(tabId);
  if ((await getState()).tabId === tabId) await stop(false);
});

// A new page in the tab gets a fresh chance at auto-start.
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) {
    needsClick.delete(tabId);
    userStopped.delete(tabId);
  }
});

// Turning auto-start off ends an auto-started session.
chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area !== "sync" || !changes.autoStart || changes.autoStart.newValue) return;
  const state = await getState();
  if (state.tabId !== null && state.source === "element") await stop(false);
});
