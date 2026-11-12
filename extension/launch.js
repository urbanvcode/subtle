// "Subtle app" when Subtle isn't running: a subtle:// link starts it (Subtle registers the link on Windows
// each time it starts; the browser asks first). This page waits until Subtle answers, asks it to show its
// window, and closes itself.
const SERVER = "ws://127.0.0.1:8765";
const $ = (id) => document.getElementById(id);

function launch() {
  location.assign("subtle://open"); // the page stays; the browser hands the link to Windows
}

function tryConnect() {
  return new Promise((resolve) => {
    let ws;
    try {
      ws = new WebSocket(SERVER);
    } catch {
      return resolve(false);
    }
    const t = setTimeout(() => {
      try { ws.close(); } catch {}
      resolve(false);
    }, 1500);
    ws.onopen = () => {
      clearTimeout(t);
      ws.send(JSON.stringify({ type: "show_panel" }));
      setTimeout(() => ws.close(), 300);
      resolve(true);
    };
    ws.onerror = () => {
      clearTimeout(t);
      resolve(false);
    };
  });
}

async function wait() {
  const start = Date.now();
  while (Date.now() - start < 60000) {
    if (await tryConnect()) {
      document.querySelector("h1").lastChild.textContent = "Subtle is running";
      $("msg").innerHTML = '<span class="ok">Its window opens now.</span> This tab closes by itself.';
      setTimeout(() => window.close(), 1200);
      return;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  $("msg").innerHTML =
    "Subtle didn't start. Start it from the <b>Subtle</b> shortcut on the desktop or in the Start menu " +
    "(on Linux or macOS: <code>subtle.sh</code> in the Subtle folder), then press Try again.";
}

$("again").onclick = () => {
  launch();
  wait();
};
launch();
wait();
