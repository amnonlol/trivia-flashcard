// App version shown on the start screen so we know which build is live at an
// event. Bump this alongside `version` in app/package.json (CRA can't import
// package.json from outside src/). A build may override it via REACT_APP_VERSION.
const APP_VERSION = process.env.REACT_APP_VERSION || '0.1.5';

export default APP_VERSION;
