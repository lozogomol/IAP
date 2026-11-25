// Service worker de Peaje IAP (PWA / TWA). Generado por peaje/pwa.py.
// Reglas:
//  - SOLO se cachean estáticos: /static/ (CSS, JS, íconos) y las fuentes de Google. Estrategia "network first":
//    siempre se pide a la red (los cambios de diseño se ven al instante) y el caché solo se usa sin conexión.
//  - NUNCA se cachea la API (/api/), el login (/login, /auth/, /logout) ni páginas con datos: van directo a la red.
//  - Si una página no carga por falta de conexión, se muestra /offline.
"use strict";
var VERSION = "{{ version }}";
var CACHE = "iap-estaticos-" + VERSION;
var PRECACHE = {{ precache|safe }};  // JSON generado en la vista
var FUENTES = ["https://fonts.googleapis.com", "https://fonts.gstatic.com"];
var NUNCA = [/^\/api\//, /^\/auth\//, /^\/login/, /^\/logout/, /^\/pusher\//, /^\/capturas\//, /^\/admin\//];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(PRECACHE); }).then(function () {
    return self.skipWaiting();
  }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (claves) {
    return Promise.all(claves.filter(function (k) { return k.indexOf("iap-estaticos-") === 0 && k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

function esEstatico(url) {
  if (url.origin === self.location.origin) return url.pathname.indexOf("/static/") === 0;
  return FUENTES.some(function (o) { return url.origin === o; });
}

function redPrimero(pedido) {
  return fetch(pedido).then(function (r) {
    if (r && (r.ok || r.type === "opaque")) {
      var copia = r.clone();
      caches.open(CACHE).then(function (c) { c.put(pedido, copia); });
    }
    return r;
  }).catch(function () {
    return caches.match(pedido).then(function (r) { return r || Response.error(); });
  });
}

self.addEventListener("fetch", function (e) {
  var pedido = e.request;
  if (pedido.method !== "GET") return;  // POST (frames, login, chat…): siempre a la red, sin tocar
  var url = new URL(pedido.url);
  if (url.origin === self.location.origin && NUNCA.some(function (re) { return re.test(url.pathname); })) return;
  if (esEstatico(url)) { e.respondWith(redPrimero(pedido)); return; }
  if (pedido.mode === "navigate") {
    // Páginas: siempre de la red y sin guardarlas; sin conexión, la página "Sin conexión".
    e.respondWith(fetch(pedido).catch(function () { return caches.match("/offline"); }));
  }
});
