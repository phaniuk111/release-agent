#!/usr/bin/env node
// Yarn 4 ignores .npmrc. The pipelines hand over an .npmrc (the
// GCP_ARTIFACTORY_NPMRC secret), so this turns it into the YARN_* settings
// Yarn does read: registry, auth, always-auth, strict-ssl.
//
//   eval "$(node scripts/npmrc-to-yarn-env.cjs /root/.npmrc)"       # a shell step
//   node scripts/npmrc-to-yarn-env.cjs "$NPMRC" --github-env          # a GitHub Actions step
//
// A missing or empty file prints nothing: Yarn then uses public npm, through
// HTTPS_PROXY/HTTP_PROXY if set (only when there is no internal registry).
// With --github-env the values go to $GITHUB_ENV and the secret ones are masked
// in the log first. Scoped keys ("//host/path/:_auth") count as their bare key.
'use strict';
const fs = require('fs');

const [file, mode] = process.argv.slice(2);
const npmrc = {};
if (file && fs.existsSync(file)) {
  for (const raw of fs.readFileSync(file, 'utf8').split('\n')) {
    const line = raw.trim();
    const eq = line.indexOf('=');
    if (!line || line[0] === '#' || line[0] === ';' || eq < 0) continue;
    let key = line.slice(0, eq).trim();
    const colon = key.lastIndexOf(':');
    if (key.startsWith('//') && colon > 1) key = key.slice(colon + 1);
    npmrc[key] = line.slice(eq + 1).trim();
  }
}

const env = process.env;
const https = env.HTTPS_PROXY || env.https_proxy || '';
const http = env.HTTP_PROXY || env.http_proxy || https;
const out = {
  YARN_NPM_REGISTRY_SERVER: npmrc.registry,
  YARN_NPM_AUTH_IDENT: npmrc._auth,
  YARN_NPM_AUTH_TOKEN: npmrc._authToken,
  YARN_NPM_ALWAYS_AUTH: npmrc['always-auth'],
  YARN_ENABLE_STRICT_SSL: npmrc['strict-ssl'],
};
if (npmrc.registry) {
  // Native-module downloads (prebuild-install) still go through the proxy.
  out.npm_config_https_proxy = https || http;
  out.npm_config_proxy = http;
} else {
  out.YARN_HTTPS_PROXY = https || http;
  out.YARN_HTTP_PROXY = http;
}
const SECRET = new Set(['YARN_NPM_AUTH_IDENT', 'YARN_NPM_AUTH_TOKEN']);

if (mode === '--github-env') {
  const lines = [];
  for (const [k, v] of Object.entries(out)) {
    if (!v) continue;
    if (/[\r\n]/.test(v)) throw new Error(`${k} has a line break`);
    if (SECRET.has(k)) console.log(`::add-mask::${v}`);
    lines.push(`${k}=${v}`);
  }
  if (lines.length) fs.appendFileSync(env.GITHUB_ENV, lines.join('\n') + '\n');
  console.log(npmrc.registry ? 'npm registry: from the .npmrc secret' : 'npm registry: public npm');
} else {
  const q = (v) => "'" + String(v).split("'").join("'\\''") + "'";
  for (const [k, v] of Object.entries(out)) if (v) console.log(`export ${k}=${q(v)}`);
}
