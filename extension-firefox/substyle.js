// Subtitle look: one definition shared by the overlay on the video (content.js) and the popup's
// live preview (popup.js), so the preview always shows exactly what appears on the video.
// Settings live in chrome.storage.sync next to the others (fontScale, position, ...).
var SubtleStyle = (globalThis.SubtleStyle = globalThis.SubtleStyle || (() => {
  const DEFAULTS = {
    textColor: "#ffffff",
    bgColor: "#08080c",
    bgOpacity: 62, // %, 0 = no box behind the text
    edge: "soft", // none | soft | outline | drop
    font: "default",
    weight: "semibold",
    lineWidth: 90, // % of the video width a line may use
    lineTime: "normal", // short | normal | long: how long a finished line stays
    srcSize: 70, // % size of the original-language line
    keepPrev: true, // keep the previous line (dimmed) above the new one
  };

  const PRESETS = {
    classic: { label: "Classic box", textColor: "#ffffff", bgColor: "#08080c", bgOpacity: 62, edge: "soft", weight: "semibold" },
    outline: { label: "Outline", textColor: "#ffffff", bgOpacity: 0, edge: "outline", weight: "bold" },
    shadow: { label: "Soft shadow", textColor: "#ffffff", bgOpacity: 0, edge: "drop", weight: "semibold" },
    contrast: { label: "High contrast", textColor: "#ffe873", bgColor: "#000000", bgOpacity: 92, edge: "none", weight: "bold" },
  };

  const TEXT_COLORS = [["#ffffff", "White"], ["#ffe873", "Yellow"], ["#8fd8ff", "Light blue"], ["#a8f0a0", "Light green"], ["#ffb3de", "Pink"]];
  const BG_COLORS = [["#08080c", "Black"], ["#2a2d34", "Dark grey"], ["#0b1a33", "Navy"], ["#ffffff", "White"]];

  const FONTS = {
    default: ["Default", 'system-ui, "Segoe UI", "Helvetica Neue", Arial, sans-serif'],
    arial: ["Arial", 'Arial, "Helvetica Neue", Helvetica, sans-serif'],
    verdana: ["Verdana (wide, very readable)", "Verdana, Geneva, sans-serif"],
    trebuchet: ["Trebuchet", '"Trebuchet MS", "Segoe UI", sans-serif'],
    georgia: ["Georgia (serif)", 'Georgia, "Times New Roman", serif'],
    mono: ["Monospace", 'Consolas, "Cascadia Mono", "Courier New", monospace'],
  };
  const WEIGHTS = { regular: ["Regular", 500], semibold: ["Semi-bold", 600], bold: ["Bold", 750] };
  const EDGES = {
    none: ["None", "none"],
    soft: ["Soft shadow", "0 0 .12em #000, 0 0 .12em #000, 0 .06em .14em #000"],
    outline: ["Outline", [-1, 0, 1].flatMap((x) => [-1, 0, 1].map((y) => `${x * 0.055}em ${y * 0.055}em 0 #000`)).join(", ") + ", 0 .07em .1em #000"],
    drop: ["Drop shadow", "0 .08em .22em rgba(0,0,0,.95), 0 0 .55em rgba(0,0,0,.55)"],
  };
  const TIMES = { short: ["Shorter", 0.7], normal: ["Normal", 1], long: ["Longer", 1.5] };

  function rgba(hex, pct) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "") || [0, "08080c"];
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${Math.max(0, Math.min(100, +pct || 0)) / 100})`;
  }

  // CSS custom properties for a style; the stylesheet below reads them.
  function vars(s) {
    const st = { ...DEFAULTS, ...s };
    const boxed = +st.bgOpacity > 0;
    return {
      "--fg": st.textColor,
      "--bg": rgba(st.bgColor, st.bgOpacity),
      "--edge": (EDGES[st.edge] || EDGES.soft)[1],
      "--font": (FONTS[st.font] || FONTS.default)[1],
      "--weight": String((WEIGHTS[st.weight] || WEIGHTS.semibold)[1]),
      "--src": `${Math.max(40, Math.min(100, +st.srcSize || 70)) / 100}em`,
      "--pad": boxed ? ".14em .55em .2em" : ".04em .25em",
    };
  }

  function apply(el, s) {
    if (!el) return;
    for (const [k, v] of Object.entries(vars(s))) el.style.setProperty(k, v);
  }

  const CSS = `
    .box { display: flex; flex-direction: column; align-items: center; gap: .18em; }
    .line {
      max-width: 100%;
      box-sizing: border-box;
      padding: var(--pad, .14em .55em .2em);
      border-radius: .35em;
      background: var(--bg, rgba(8, 8, 12, .62));
      color: var(--fg, #fff);
      font-family: var(--font, system-ui, "Segoe UI", Arial, sans-serif);
      font-weight: var(--weight, 600);
      font-size: 1em;
      line-height: 1.28;
      text-align: center;
      text-wrap: balance;
      text-shadow: var(--edge, 0 0 .12em #000);
      -webkit-box-decoration-break: clone;
      box-decoration-break: clone;
    }
    .line:empty { display: none; }
    .prev { font-size: .82em; opacity: .7; }
    .partial { opacity: .85; font-style: italic; }
    .src { font-size: var(--src, .7em); font-weight: 500; opacity: .9; margin-top: -.1em; }`;

  const lineTime = (s) => (TIMES[(s || {}).lineTime] || TIMES.normal)[1];
  const KEYS = Object.keys(DEFAULTS);

  return { DEFAULTS, PRESETS, TEXT_COLORS, BG_COLORS, FONTS, WEIGHTS, EDGES, TIMES, KEYS, CSS, vars, apply, lineTime };
})());
