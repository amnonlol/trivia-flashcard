/* eslint-disable no-restricted-globals */

import { clientsClaim } from 'workbox-core';
import { ExpirationPlugin } from 'workbox-expiration';
import { precacheAndRoute, createHandlerBoundToURL } from 'workbox-precaching';
import { registerRoute } from 'workbox-routing';
import { StaleWhileRevalidate } from 'workbox-strategies';

clientsClaim();

precacheAndRoute(self.__WB_MANIFEST);

const fileExtensionRegexp = new RegExp('/[^/?]+\\.[^/]+$');
registerRoute(({ request, url }) => {
  if (request.mode !== 'navigate') {
    return false;
  }

  if (url.pathname.startsWith('/_')) {
    return false;
  }

  if (url.pathname.match(fileExtensionRegexp)) {
    return false;
  }

  return true;
}, createHandlerBoundToURL(process.env.PUBLIC_URL + '/index.html'));

registerRoute(
  ({ url }) =>
    url.origin === self.location.origin && url.pathname.endsWith('.png'),
  new StaleWhileRevalidate({
    cacheName: 'images',
    plugins: [new ExpirationPlugin({ maxEntries: 50 })]
  })
);

// The One Piece question bank is the entire content of the app, so it must be
// available offline for the PWA promise to hold. Precaching via __WB_MANIFEST
// isn't guaranteed — the bank can exceed CRA's default precache size cap — so we
// register an explicit runtime route. StaleWhileRevalidate serves the cached copy
// instantly (fast, offline-safe) while refreshing it in the background, so a
// redeployed bank is picked up on the next visit without a version bump.
registerRoute(
  ({ url }) =>
    url.origin === self.location.origin &&
    url.pathname.endsWith('/data/questions.json'),
  new StaleWhileRevalidate({
    cacheName: 'question-bank',
    plugins: [new ExpirationPlugin({ maxEntries: 4 })]
  })
);

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
