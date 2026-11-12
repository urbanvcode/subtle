// Content script (every frame): reports play / pause / seek of the page's video and
// draws the subtitle overlay over it, including in fullscreen.
//
// It is declared in the manifest AND injected on Start (for tabs opened before the
// extension was loaded), so it must tolerate running twice and replace a copy left
// behind by a reloaded extension.
(() => {
  const alive = () => {
    try {
      return !!chrome.runtime?.id;
    } catch {
      return false;
    }
  };
  const prev = window.__subtleSubs;
  if (prev && prev.isAlive()) return; // already running in this frame
  if (prev) {
    try {
      prev.destroy(); // orphan from a reloaded extension
    } catch {}
  }

  const ac = new AbortController();
  const on = (target, type, fn, opts = {}) => target.addEventListener(type, fn, { ...opts, signal: ac.signal });

  const STYLE = globalThis.SubtleStyle; // substyle.js, loaded just before this file
  const SETTINGS = { fontScale: 1, showPartial: true, position: 8, showOriginal: false, autoStart: false,
    ...(STYLE ? STYLE.DEFAULTS : {}) };
  let settings = { ...SETTINGS };
  let active = false;
  let video = null; // the video being watched in this frame
  let host = null;
  let prevEl = null; // previous finished line (dimmed)
  let curEl = null; // current line
  let boxEl = null; // holds the lines; carries the style (CSS variables from substyle.js)
  let srcEl = null; // original-language text of the current line (optional)
  let hideTimer = null;
  let prevTimer = null;
  let rafId = 0;
  let lastFinalId = -1;
  let lastFinalText = "";
  const norm = (t) => (t || "").toLowerCase().replace(/[\s.!?。、,！？…]+/g, " ").trim();
  let scanTimer = 0;
  let posKey = ""; // last geometry written to the overlay (styles are only touched when it changes)

  chrome.storage.sync.get(SETTINGS).then((s) => {
    settings = { ...SETTINGS, ...s };
    applyStyle();
  });
  const onStorage = (changes, area) => {
    if (area !== "sync") return;
    for (const [k, v] of Object.entries(changes)) if (k in SETTINGS) settings[k] = v.newValue;
    if (STYLE && STYLE.KEYS.some((k) => k in changes)) applyStyle();
    position();
    if (changes.autoStart && changes.autoStart.newValue && document.visibilityState === "visible") {
      scan();
      const v = pickVideo();
      if (v && !v.paused) maybeAuto(v);
    }
  };
  chrome.storage.onChanged.addListener(onStorage);

  // ---- finding videos (including inside open shadow roots) ------------------
  const hooked = new WeakSet();
  let knownVideos = [];

  function deepVideos(root, out = []) {
    out.push(...root.querySelectorAll("video"));
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    for (let n = walker.currentNode; n; n = walker.nextNode()) {
      if (n.shadowRoot) deepVideos(n.shadowRoot, out);
    }
    return out;
  }

  function scan() {
    knownVideos = deepVideos(document);
    for (const v of knownVideos) hook(v);
  }

  // Listen on each video directly so players inside shadow DOM are covered too.
  function hook(v) {
    if (hooked.has(v)) return;
    hooked.add(v);
    on(v, "play", () => {
      if (sideShow(v)) return;
      video = v;
      report("play");
    });
    on(v, "playing", () => {
      if (video === v || !video) report("play");
      maybeAuto(v);
    });
    on(v, "volumechange", () => {
      if (!v.paused) maybeAuto(v); // unmuting a playing video counts as "starts playing with sound"
    });
    on(v, "pause", () => {
      if (video === v || !video) {
        video = v;
        onPaused();
      }
    });
    on(v, "ended", () => {
      if (video === v) onPaused();
    });
    on(v, "seeking", () => {
      if (video && video !== v && !video.paused) return;
      video = v;
      clearAll();
      report("seek");
    });
  }

  // A silent clip that starts while the watched video is playing (hover preview, ad, background
  // loop) must not take over: its pause would otherwise stop the subtitles of the real video.
  function sideShow(v) {
    return !!video && video !== v && video.isConnected && !video.paused && !video.ended && (v.muted || v.volume === 0);
  }

  function visibleArea(v) {
    const r = v.getBoundingClientRect();
    return r.width > 40 && r.height > 30 ? r.width * r.height : 0;
  }

  function pickVideo() {
    if (video && video.isConnected && visibleArea(video)) return video;
    let best = null;
    let bestScore = 0;
    for (const v of knownVideos) {
      if (!v.isConnected) continue;
      const score = visibleArea(v) * (v.paused ? 1 : 4);
      if (score > bestScore) {
        best = v;
        bestScore = score;
      }
    }
    return best || (video && video.isConnected ? video : null);
  }

  const isPaused = () => !!video && video.paused && !video.ended;

  function report(event) {
    if (!active || !alive()) return;
    chrome.runtime.sendMessage({ cmd: "video", event }).catch(() => {});
  }

  function onPaused() {
    report("pause");
    // Drop the in-progress line; keep the last finished line on screen while paused.
    if (curEl && curEl.classList.contains("partial")) setLine(curEl, "");
    refreshVisibility();
  }

  // Catch videos added later (SPAs, players that load lazily).
  scan();
  on(
    document,
    "play",
    (e) => {
      const v = e.target;
      if (!(v instanceof HTMLVideoElement) || hooked.has(v)) return; // hooked videos report themselves
      hook(v);
      if (sideShow(v)) return;
      video = v;
      report("play");
    },
    { capture: true }
  );
  // Rescanning walks the whole page, so it only happens in the tab that is being subtitled.
  const scanWanted = () => active || (settings.autoStart && document.visibilityState === "visible");
  const mo = new MutationObserver(() => {
    if (scanWanted() && !scanTimer) scanTimer = setTimeout(() => ((scanTimer = 0), scan()), 400);
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
  const periodic = setInterval(() => scanWanted() && scan(), 3000); // shadow roots don't trigger the observer

  // ---- auto-start: audio straight from the page's video (no click needed) ----------------
  // Tab capture needs a click on the extension, so auto-start reads the audio from the <video>
  // element instead (captureStream). That works for most players (YouTube and other
  // stream-based players); DRM video and cross-origin files without CORS can't be read, and
  // then the extension falls back to "click to subtitle".
  let ecap = null; // { video, stream, ctx, src, node, port, zeroFrames }
  let autoSent = 0;

  function audible(v) {
    return !v.muted && v.volume > 0 && visibleArea(v) >= 200 * 120;
  }

  function maybeAuto(v) {
    if (!settings.autoStart || !alive() || !audible(v) || sideShow(v)) return;
    if (ecap) {
      if (ecap.video !== v) startElementCapture(v); // a new video in this frame: follow it
      return;
    }
    const now = Date.now();
    if (now - autoSent < 1500) return;
    autoSent = now;
    chrome.runtime.sendMessage({ cmd: "auto-play", drm: !!v.mediaKeys }).catch(() => {});
  }

  function elementFailed(reason) {
    stopElementCapture();
    if (alive()) chrome.runtime.sendMessage({ cmd: "element-failed", reason }).catch(() => {});
  }

  function startElementCapture(v) {
    stopElementCapture();
    if (!v) return elementFailed("no-video");
    if (v.mediaKeys) return elementFailed("drm");
    let stream;
    try {
      stream = (v.captureStream || v.mozCaptureStream).call(v);
    } catch (e) {
      return elementFailed("blocked");
    }
    const port = chrome.runtime.connect({ name: "subtle-audio" });
    const ctx = new AudioContext({ sampleRate: 16000 });
    const st = { video: v, stream, ctx, src: null, node: null, port, zeroFrames: 0 };
    ecap = st;
    video = v;
    port.onDisconnect.addListener(() => {
      if (ecap === st) stopElementCapture();
    });
    const attach = () => {
      if (ecap !== st || st.src || !stream.getAudioTracks().length) return;
      st.src = ctx.createMediaStreamSource(stream);
      // ScriptProcessor (not a worklet): worklets load a script file, which some sites' security
      // policies block. 512 samples at 16 kHz = 32 ms, the same chunks the tab capture sends.
      st.node = ctx.createScriptProcessor(512, 2, 1);
      st.node.onaudioprocess = (e) => {
        if (ecap !== st) return;
        const inp = e.inputBuffer;
        const n = inp.length;
        const out = new Float32Array(n);
        let peak = 0;
        for (let c = 0; c < inp.numberOfChannels; c++) {
          const d = inp.getChannelData(c);
          for (let i = 0; i < n; i++) out[i] += d[i] / inp.numberOfChannels;
        }
        for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(out[i]));
        // Media the page may play but not read (cross-origin) comes through as exact silence.
        if (peak === 0 && !v.paused && !v.muted && v.volume > 0) {
          if (++st.zeroFrames > 190) return elementFailed("silent"); // ~6 s of digital silence
        } else st.zeroFrames = 0;
        const bytes = new Uint8Array(out.buffer);
        let bin = "";
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        try {
          port.postMessage(btoa(bin));
        } catch {
          stopElementCapture();
        }
      };
      st.src.connect(st.node);
      st.node.connect(ctx.destination); // needed for it to run; it outputs silence, the page plays the sound
    };
    stream.addEventListener("addtrack", attach);
    attach();
    ctx.resume().catch(() => {});
    setTimeout(() => {
      if (ecap === st && ctx.state !== "running") elementFailed("needs-click"); // autoplay policy
    }, 2000);
    setTimeout(() => {
      if (ecap === st && !st.src) elementFailed("no-audio"); // the video has no audio track we can read
    }, 6000);
  }

  function stopElementCapture() {
    const st = ecap;
    if (!st) return;
    ecap = null;
    try {
      st.node && (st.node.onaudioprocess = null);
      st.src && st.src.disconnect();
      st.node && st.node.disconnect();
    } catch {}
    st.ctx.close().catch(() => {});
    try {
      st.port.disconnect();
    } catch {}
  }

  // ---- overlay ------------------------------------------------------------
  function ensureHost() {
    if (host && host.isConnected) return;
    host = document.createElement("subtle-subtitles");
    posKey = "";
    host.setAttribute("popover", "manual");
    host.setAttribute(
      "style",
      [
        "all: initial",
        "position: fixed !important",
        "inset: auto !important",
        "margin: 0 !important",
        "padding: 0 !important",
        "border: 0 !important",
        "background: transparent !important",
        "overflow: visible !important",
        "pointer-events: none !important",
        "z-index: 2147483647 !important",
        "display: none",
      ].join(";")
    );
    const root = host.attachShadow({ mode: "closed" });
    // The look (colours, box, edge, font) comes from substyle.js, shared with the popup's preview.
    const css = STYLE ? STYLE.CSS : `
        .box { display: flex; flex-direction: column; align-items: center; gap: .18em; }
        .line { max-width: 100%; box-sizing: border-box; padding: .14em .55em .2em; border-radius: .35em;
          background: rgba(8, 8, 12, .62); color: #fff; font: 600 1em/1.28 system-ui, "Segoe UI", Arial, sans-serif;
          text-align: center; text-wrap: balance; text-shadow: 0 0 .12em #000, 0 0 .12em #000, 0 .06em .14em #000; }
        .line:empty { display: none; }
        .prev { font-size: .82em; opacity: .7; }
        .partial { opacity: .85; font-style: italic; }
        .src { font-size: .7em; font-weight: 500; opacity: .9; margin-top: -.1em; }`;
    root.innerHTML = `<style>${css}</style>
      <div class="box"><div class="line prev"></div><div class="line cur"></div><div class="line src"></div></div>`;
    boxEl = root.querySelector(".box");
    prevEl = root.querySelector(".prev");
    curEl = root.querySelector(".cur");
    srcEl = root.querySelector(".src");
    applyStyle();
    document.documentElement.appendChild(host);
  }

  function applyStyle() {
    if (STYLE && boxEl) STYLE.apply(boxEl, settings);
  }

  // Re-showing a popover puts it on top of the top layer, i.e. above a fullscreen element.
  function raise() {
    if (!host || !host.isConnected) return;
    try {
      if (host.matches(":popover-open")) host.hidePopover();
      host.showPopover();
    } catch {}
    position();
  }
  on(document, "fullscreenchange", () => setTimeout(raise, 50), { capture: true });

  // While a tab is captured, the browser keeps page fullscreen inside the window. The top frame
  // asks the extension to fullscreen the whole window too (iframe players surface here as well).
  if (window === window.top) {
    on(document, "fullscreenchange", () => {
      if (!active || !alive()) return;
      chrome.runtime.sendMessage({ cmd: "fullscreen", on: !!document.fullscreenElement }).catch(() => {});
    });
  }
  on(document, "webkitfullscreenchange", () => setTimeout(raise, 50), { capture: true });

  const hasText = () => !!(curEl && (curEl.textContent || prevEl.textContent));

  function position() {
    if (!host || !hasText()) return;
    const v = pickVideo();
    let r = v ? v.getBoundingClientRect() : null;
    if (!r || r.width < 40 || r.height < 30) r = { left: 0, top: 0, width: innerWidth, height: innerHeight };
    const left = Math.max(0, r.left);
    const right = Math.min(innerWidth, r.left + r.width);
    const bottom = Math.min(innerHeight, r.top + r.height);
    const width = Math.max(160, right - left);
    const h = Math.min(r.height, innerHeight);
    const fontPx = Math.max(14, Math.min(64, h * 0.05 * settings.fontScale));
    const share = Math.max(0.4, Math.min(1, (+settings.lineWidth || 90) / 100)); // "Line width" setting
    const widthPx = Math.round(width * share);
    const leftPx = Math.round(left + (width - widthPx) / 2);
    const bottomPx = Math.round(innerHeight - bottom + h * (settings.position / 100));
    // This runs every frame while a line is up; only write styles when the video actually moved.
    const key = `${leftPx}|${widthPx}|${bottomPx}|${fontPx}`;
    if (key === posKey) return;
    posKey = key;
    host.style.setProperty("left", `${leftPx}px`, "important");
    host.style.setProperty("width", `${widthPx}px`, "important");
    host.style.setProperty("top", "auto", "important");
    host.style.setProperty("bottom", `${bottomPx}px`, "important");
    host.style.setProperty("font-size", `${fontPx}px`, "important");
  }

  function loop() {
    position();
    rafId = hasText() ? requestAnimationFrame(loop) : 0;
  }

  // One colour per speaker (when "Identify speakers" is on); unknown speakers stay white.
  const SPEAKER_COLOURS = ["#ffe873", "#8fd8ff", "#a8f0a0", "#ffb3de", "#ffc285", "#cdb8ff", "#9ff0e6", "#f5f5f5"];
  const speakerColour = (id) => (id ? SPEAKER_COLOURS[(id - 1) % SPEAKER_COLOURS.length] : "");

  function setLine(el, text) {
    el.textContent = text;
    if (!text) el.style.color = "";
    if (el === curEl && !text) {
      if (srcEl) srcEl.textContent = ""; // original goes with its line
      // Never leave the older line on its own: it would drop into the main slot.
      clearTimeout(prevTimer);
      if (prevEl) prevEl.textContent = "";
    }
  }

  function setSrc(src) {
    if (srcEl) srcEl.textContent = settings.showOriginal && src ? src : "";
  }

  function refreshVisibility() {
    if (!host) return;
    if (hasText()) {
      host.style.setProperty("display", "block", "important");
      if (!host.matches(":popover-open")) raise();
      if (!rafId) rafId = requestAnimationFrame(loop);
    } else {
      host.style.setProperty("display", "none", "important");
    }
  }

  // Move the finished line on screen up to the dimmed slot; it fades after a few seconds.
  function moveCurrentUp() {
    if (!curEl.textContent || curEl.classList.contains("partial")) return;
    if (settings.keepPrev === false) return; // "Keep the previous line" off: the new line simply replaces it
    setLine(prevEl, curEl.textContent);
    prevEl.style.color = curEl.style.color; // the line keeps its speaker's colour
    clearTimeout(prevTimer);
    prevTimer = setTimeout(function hidePrev() {
      if (isPaused()) return (prevTimer = setTimeout(hidePrev, 500));
      setLine(prevEl, "");
      refreshVisibility();
    }, 4000);
  }

  function showFinal(text, src, speaker) {
    ensureHost();
    moveCurrentUp();
    curEl.classList.remove("partial");
    setLine(curEl, text);
    curEl.style.color = speakerColour(speaker);
    setSrc(src);
    raise();
    refreshVisibility();
    clearTimeout(hideTimer);
    const ms = Math.min(7000, Math.max(2200, text.length * 70)) * (STYLE ? STYLE.lineTime(settings) : 1);
    hideTimer = setTimeout(function hideCur() {
      if (isPaused()) return (hideTimer = setTimeout(hideCur, 500));
      // The older line above must never outlive the current one, or it would drop
      // down into the main slot and look like the old subtitle came back.
      clearTimeout(prevTimer);
      setLine(prevEl, "");
      setLine(curEl, "");
      refreshVisibility();
    }, ms);
  }

  function showPartial(text, src) {
    ensureHost();
    moveCurrentUp(); // keep the last finished line visible above
    clearTimeout(hideTimer);
    curEl.classList.add("partial");
    setLine(curEl, text);
    curEl.style.color = ""; // speaker is known only once the line is finished
    setSrc(src);
    refreshVisibility();
    // Normally the finished line replaces a preview within a second or two. If it never comes
    // (server stopped, connection lost), don't leave the preview on screen for ever.
    hideTimer = setTimeout(function hidePartial() {
      if (isPaused()) return (hideTimer = setTimeout(hidePartial, 500));
      curEl.classList.remove("partial");
      setLine(curEl, ""); // also clears the older line above (it must never outlive the current one)
      refreshVisibility();
    }, 8000);
  }

  function clearAll() {
    lastFinalText = "";
    clearTimeout(hideTimer);
    clearTimeout(prevTimer);
    if (curEl) {
      setLine(curEl, "");
      setLine(prevEl, "");
    }
    refreshVisibility();
  }

  // ---- messages from the extension ---------------------------------------
  function onMessage(msg) {
    switch (msg.type) {
      case "active":
        active = msg.active;
        lastFinalId = -1; // a new capture is a new server connection
        if (active) scan();
        else clearAll();
        break;
      case "query": {
        scan();
        const v = pickVideo();
        if (v) {
          video = v;
          report(v.paused ? "pause" : "play");
        }
        break;
      }
      case "sub":
        if (isPaused()) return; // nothing new while paused
        if (msg.final) {
          lastFinalId = msg.id; // finished lines arrive in order; ids start over if the server restarts
          if (msg.text) {
            lastFinalText = msg.text;
            showFinal(msg.text, msg.src, msg.speaker);
          } else if (curEl && curEl.classList.contains("partial")) {
            // The line turned out to be nothing (noise, dropped): don't leave its preview behind.
            setLine(curEl, "");
            curEl.classList.remove("partial");
            refreshVisibility();
          }
        } else if (
          settings.showPartial &&
          msg.id > lastFinalId &&
          msg.text &&
          norm(msg.text) !== norm(lastFinalText) // a preview that repeats the last line
        ) {
          showPartial(msg.text, msg.src);
        }
        break;
      case "clear":
        clearAll();
        break;
      case "element-start": {
        scan();
        const v = video && !video.paused ? video : pickVideo();
        startElementCapture(v);
        break;
      }
      case "element-stop":
        stopElementCapture();
        break;
    }
  }
  chrome.runtime.onMessage.addListener(onMessage);

  window.__subtleSubs = {
    isAlive: alive,
    destroy() {
      stopElementCapture();
      ac.abort();
      mo.disconnect();
      clearInterval(periodic);
      clearAll();
      if (host) host.remove();
      try {
        chrome.runtime.onMessage.removeListener(onMessage);
        chrome.storage.onChanged.removeListener(onStorage);
      } catch {}
    },
  };

  chrome.runtime
    .sendMessage({ cmd: "am-i-active" })
    .then((r) => {
      active = !!(r && r.active);
      if (active) {
        scan();
        const v = pickVideo();
        if (v) report(v.paused ? "pause" : "play");
      }
    })
    .catch(() => {});
})();
