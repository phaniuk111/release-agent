// Every form card the chat can open. One module per screen lives in forms/;
// their rules live in core/ (pure, unit-tested) and every backend call in
// api.js. This index keeps the import path the rest of the UI has always used.
//
// Layering, and what a React port changes (see static/README.md):
//   core/*.js   rules + wording        → imported unchanged
//   api.js      backend contract       → imported unchanged (or mirrored)
//   forms/*.js  DOM rendering + state  → rewritten as components
export { showQueueForm } from './forms/queue_form.js';
export { showQueueTable } from './forms/queue_table.js';
export { showReleaseForm } from './forms/release_form.js';
export { showDfDeployForm } from './forms/df_deploy_form.js';
export { showDeployForm } from './forms/deploy_form.js';
export { parseDeployIntent, parseDeployInclude } from './forms/parse.js';
export { queueDestination } from './core/queue.js';
