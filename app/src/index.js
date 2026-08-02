import React from 'react';
import ReactDOM from 'react-dom/client';
import './index.css';
import App from './components/App';
import UpdatePrompt from './components/UpdatePrompt';
import { ProfileProvider } from './context/ProfileContext';

const root = ReactDOM.createRoot(document.getElementById('root'));

// UpdatePrompt registers the service worker and offers a reload when a new build
// is waiting — see its comment for why the default registration strands players
// on a stale build.
root.render(
  <React.StrictMode>
    <ProfileProvider>
      <App />
      <UpdatePrompt />
    </ProfileProvider>
  </React.StrictMode>
);
