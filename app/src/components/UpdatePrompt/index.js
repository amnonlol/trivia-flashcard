import React, { useEffect, useRef, useState } from 'react';
import { Button } from 'semantic-ui-react';

import * as serviceWorkerRegistration from '../../serviceWorkerRegistration';

/**
 * Offers a reload when a new build is waiting.
 *
 * Without this, a new service worker downloads but sits in *waiting* until every
 * tab for the page is closed — which on an installed PWA means the app has to be
 * swiped out of the app switcher, not just backgrounded. Players stay on a stale
 * build indefinitely and never find out there is a newer one.
 *
 * Registration lives here rather than in index.js so the thing that reacts to an
 * update is the same thing that asks for it.
 */
const UpdatePrompt = () => {
  const [waitingWorker, setWaitingWorker] = useState(null);
  const reloading = useRef(false);

  useEffect(() => {
    serviceWorkerRegistration.register({
      onUpdate: registration => setWaitingWorker(registration.waiting),
    });
  }, []);

  if (!waitingWorker) return null;

  const reload = () => {
    // The waiting worker takes control first, then we reload, so the page comes
    // back on the new build in one step. `controllerchange` can fire more than
    // once, hence the guard — a second reload would look like a crash loop.
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (reloading.current) return;
      reloading.current = true;
      window.location.reload();
    });
    waitingWorker.postMessage({ type: 'SKIP_WAITING' });
  };

  return (
    <div className="op-update-prompt" role="status">
      <span>A new version is available.</span>
      <Button size="tiny" primary compact onClick={reload}>
        Reload
      </Button>
    </div>
  );
};

export default UpdatePrompt;
