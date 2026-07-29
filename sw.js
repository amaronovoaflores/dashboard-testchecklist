// Service worker generico del dashboard de flotas — igual en todos los
// clientes (se copia via onboarding, como index.html/CNAME/updater.py).
// No editar por cliente.
var CACHE_NAME = 'flotas-shell-v1';
var APP_SHELL = ['./', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', function(e){
  e.waitUntil(caches.open(CACHE_NAME).then(function(c){ return c.addAll(APP_SHELL); }));
  self.skipWaiting();
});

self.addEventListener('activate', function(e){
  e.waitUntil(
    caches.keys().then(function(keys){
      return Promise.all(keys.filter(function(k){ return k!==CACHE_NAME; }).map(function(k){ return caches.delete(k); }));
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function(e){
  var url = e.request.url;
  // config.json y datos_*.json cambian cada noche: siempre intenta traer
  // lo mas fresco, solo cae a cache si no hay internet (ej. en campo sin señal).
  var esDatos = url.indexOf('config.json')>-1 || url.indexOf('datos_')>-1 || url.indexOf('/app.html')>-1;
  if (esDatos){
    e.respondWith(
      fetch(e.request).then(function(resp){
        var copy = resp.clone();
        caches.open(CACHE_NAME).then(function(c){ c.put(e.request, copy); });
        return resp;
      }).catch(function(){ return caches.match(e.request); })
    );
  } else {
    // Shell (loader, iconos, manifest, librerias CDN): cache-first para
    // que abra al instante, refresca la cache en segundo plano.
    e.respondWith(
      caches.match(e.request).then(function(cached){
        var fetchPromise = fetch(e.request).then(function(resp){
          var copy = resp.clone();
          caches.open(CACHE_NAME).then(function(c){ c.put(e.request, copy); });
          return resp;
        }).catch(function(){ return cached; });
        return cached || fetchPromise;
      })
    );
  }
});
