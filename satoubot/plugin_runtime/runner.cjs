// This context only provides Nuvio compatibility. Node permissions and node:vm
// do not isolate provider code or network access; bubblewrap provides isolation.
const vm = require('node:vm');
const readline = require('node:readline');
const allowed = { 'cheerio-without-node-native': require('cheerio-without-node-native'), 'crypto-js': require('crypto-js') };
const pending = new Map();
let nextId = 0;
let started = false;
const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
function send(message) { process.stdout.write(JSON.stringify(message) + '\n'); }
function brokerFetch(url, options = {}) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, {resolve, reject});
    send({type: 'fetch', id, url: String(url), method: String(options.method || 'GET').toUpperCase(),
      headers: options.headers || {}, body: options.body == null ? null : String(options.body)});
  }).then(result => {
    const body = result.body;
    const values = new Map(Object.entries(result.headers || {}));
    const headers = {get: name => values.get(String(name).toLowerCase()) || null};
    return {ok: result.status >= 200 && result.status < 300, status: result.status,
      statusText: result.statusText, url: result.url, headers,
      text: async () => body, json: async () => JSON.parse(body)};
  });
}
lines.on('line', async line => {
  try {
    const input = JSON.parse(line);
    if (started) {
      const request = pending.get(input.id);
      if (!request) return;
      pending.delete(input.id);
      if (input.error) request.reject(new Error(input.error));
      else request.resolve(input.response);
      return;
    }
    started = true;
    const context = vm.createContext({
      module: {exports: {}}, exports: {},
      require: name => { if (!Object.hasOwn(allowed, name)) throw new Error('Unsupported module'); return allowed[name]; },
      fetch: brokerFetch,
      console: {log() {}, warn() {}, error() {}}, setTimeout, clearTimeout,
      URL, URLSearchParams, TextEncoder, TextDecoder, atob, btoa,
      SCRAPER_SETTINGS: {},
    });
    new vm.Script(input.code).runInContext(context, {timeout: 2000});
    context.tmdb = input.tmdb;
    const results = await new vm.Script('module.exports.getStreams(tmdb, "movie", null, null)').runInContext(context, {timeout: 2000});
    const output = JSON.stringify({type: 'result', results});
    if (output.length > 2000000) throw new Error('Too many results');
    process.stdout.write(output + '\n', () => process.exit(0));
  } catch { process.exit(1); }
});
