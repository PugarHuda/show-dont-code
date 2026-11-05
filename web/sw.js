// Offline at the bench: pages, run data and bench-card drawings already seen are kept, so a phone propped next to the
// pipettes still shows the steps and drawings when the lab's Wi-Fi drops. Network first, the cache only as a fallback;
// videos (large, range requests), uploads and every non-GET request always go to the network, and a private run
// (u-, c-, p-) is never cached, so a shared lab computer keeps nothing of a visitor's own work.
const CACHE = "sdc-v2";  // a new name drops what v1 kept, private runs included
const API = "https://pugarhuda--show-dont-code-web.us-east.modal.run";

self.addEventListener("install", (e) => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then((c) => c.addAll(["/", "/app"]))); });
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())));

self.addEventListener("fetch", (e) => {
  const r = e.request, u = new URL(r.url);
  const ours = u.origin === self.location.origin || u.origin === API;
  if (r.method !== "GET" || !ours || r.headers.has("range") || /\.mp4$|\/video$|\/progress$|\/mcp/.test(u.pathname)) return;
  // a visitor's upload, edited copy or import is private (unguessable link): never kept on the device
  const priv = /\/api\/runs\/[ucp]-/.test(u.pathname) || /^[ucp]-/.test(u.searchParams.get("run") || "");
  e.respondWith(fetch(r).then((res) => {
    const cc = res.headers.get("cache-control") || "";
    if (res.ok && !priv && !/no-store|private/.test(cc)) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(r, copy)); }
    return res;
  }).catch(() => caches.match(r, { ignoreVary: true }).then((hit) => hit || caches.match(u.pathname === "/app" ? "/app" : r))
    .then((hit) => hit || new Response("Offline, and this page was not opened before on this device.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } }))));
});
