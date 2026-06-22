/* Service Worker do Portal SWN — cache offline dos arquivos estáticos.
   Só funciona quando o portal é servido por http(s) (ex.: Render); de file:// o navegador não registra SW. */
const CACHE = 'swn-portal-v1';
const ASSETS = [
  './',
  'PORTAL_SWN.html',
  'CHECKLIST_LOJA_SWN.html',
  'AVALIACAO_COLABORADOR_SWN.html',
  'KIT_ADMISSAO_SWN.html',
  'RH_DISCIPLINA_JORNADA_SWN.html',
  'VENDAS_SWN.html',
  'GESTAO_SWN.html',
  'TREINAMENTOS_SWN.html',
  'NPS_SWN.html',
  'GUIA_DE_TUDO_SWN.html',
  'cofre_central.html',
  'conciliacao_3vias.html',
  'deposito.html',
  'icon-192.png',
  'icon-512.png',
  'manifest.json'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => Promise.allSettled(ASSETS.map(a => c.add(a)))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

/* Cache-first para os estáticos; rede como fallback e atualização em segundo plano. */
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then(cached => {
      const network = fetch(e.request).then(resp => {
        if (resp && resp.status === 200 && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return resp;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
