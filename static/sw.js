const CACHE = "lexico-v2";
const PRECACHE = ["/", "/static/index.html", "/static/audio.js"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(ks => Promise.all(
      ks.filter(k => k !== CACHE).map(k => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // API calls: network only (never cache game state)
  if (url.pathname.startsWith("/api/")) return;
  // HTML (o app shell): network-first. Cache-first aqui faria o usuário
  // continuar rodando o index.html antigo depois de cada deploy; o cache
  // vira só fallback offline.
  if (e.request.mode === "navigate" || url.pathname === "/" ||
      url.pathname.endsWith(".html")) {
    e.respondWith(
      fetch(e.request).then(resp => {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return resp;
      }).catch(() => caches.match(e.request).then(r => r || caches.match("/")))
    );
    return;
  }
  // Demais estáticos: cache-first
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
      if (resp.ok && url.pathname.startsWith("/static/")) {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return resp;
    }))
  );
});
