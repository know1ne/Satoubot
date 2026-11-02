// Nuvio compatibility context. Node permissions restrict filesystem and process access.
const vm = require('node:vm');
const allowed = { 'cheerio-without-node-native': require('cheerio-without-node-native'), 'crypto-js': require('crypto-js') };
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 9000000) process.exit(1); });
process.stdin.on('end', async () => {
  try {
    const {code, tmdb} = JSON.parse(input);
    const context = vm.createContext({
      module: {exports: {}}, exports: {},
      require: name => { if (!Object.hasOwn(allowed, name)) throw new Error('Unsupported module'); return allowed[name]; },
      fetch: async (url, options = {}) => {
        if (!/^https?:\/\//i.test(String(url))) throw new Error('Unsupported URL');
        const response = await fetch(url, {...options, signal: AbortSignal.timeout(20000)});
        const reader = response.body.getReader(); let size = 0; const chunks = [];
        while (true) { const {value, done} = await reader.read(); if (done) break;
          size += value.length; if (size > 8000000) { await reader.cancel(); throw new Error('Response too large'); } chunks.push(value); }
        const body = Buffer.concat(chunks).toString('utf8');
        return {ok: response.ok, status: response.status, statusText: response.statusText, url: response.url,
          headers: response.headers, text: async () => body, json: async () => JSON.parse(body)};
      },
      console: {log() {}, warn() {}, error() {}}, setTimeout, clearTimeout,
      URL, URLSearchParams, TextEncoder, TextDecoder, atob, btoa,
      SCRAPER_SETTINGS: {},
    });
    new vm.Script(code).runInContext(context, {timeout: 2000});
    context.tmdb = tmdb;
    const results = await new vm.Script('module.exports.getStreams(tmdb, "movie", null, null)').runInContext(context, {timeout: 2000});
    const output = JSON.stringify(results);
    if (output.length > 2000000) throw new Error('Too many results');
    process.stdout.write(output, () => process.exit(0));
  } catch { process.exit(1); }
});
