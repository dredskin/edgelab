// Offline shell: cache the app; always try the network first for data.
const SHELL = "edgelab-v2";
self.addEventListener("install", e => e.waitUntil(caches.open(SHELL).then(c => c.addAll(["/", "/manifest.webmanifest", "/icon.svg"]))));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); return r; })
      .catch(() => caches.match(e.request)));
  } else {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
