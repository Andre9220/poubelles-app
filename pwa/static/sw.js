// Service worker volontairement sans cache.
//
// Sa seule raison d'être est de rendre l'application installable : Chrome
// exige un gestionnaire « fetch » pour proposer l'installation. On laisse
// toutes les requêtes passer au réseau — mettre en cache les fichiers de
// Streamlit exposerait à servir une version périmée après un redéploiement.

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', () => {
  // Pas de respondWith : le navigateur applique son comportement habituel.
});
