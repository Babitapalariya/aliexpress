const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const code = html.slice(html.indexOf('  async function importOne(id){'), html.indexOf('  async function importAll(){'));
function setup(fetch) {
  const context = vm.createContext({API:'/api', encodeURIComponent,
    fp: {'123': {status:'ready'}}, fetch, drawCard() {}, showToast() {}, loadStats() {}, updatePendingBadge() {}});
  vm.runInContext(code, context);
  return context;
}
const response = (status, data) => ({status, ok:status === 200, json:async()=>data});
(async () => {
  let complete, calls = 0;
  const context = setup(() => { calls++; return new Promise(resolve => { complete = resolve; }); });
  const first = context.importOne('123');
  await context.importOne('123');
  assert.equal(calls, 1); // Double-click/bulk overlap cannot start another POST.
  complete(response(200, {status:'imported', shopify_product:{id:'456'}}));
  await first;
  await context.importOne('123');
  assert.equal(calls, 1);
  assert.equal(context.fp['123'].status, 'ok');

  for (const [rows, expected] of [
    [[{source:'imported', aliexpress_id:'123', shopify_product_id:'456'}], 'ok'],
    [[{source:'mapping', aliexpress_id:'123', shopify_product_id:'456'}], 'skip'],
    [[{source:'imported', aliexpress_id:'1234', shopify_product_id:'456'}], 'skip'],
    [[{source:'imported', aliexpress_id:'123', shopify_product_id:null}], 'skip'],
    [[], 'skip'],
  ]) {
    const retry = setup(async url => url.includes('/lookup?') ? response(200, {results:rows}) : response(409, {detail:'Product already imported to Shopify'}));
    await retry.importOne('123');
    assert.equal(retry.fp['123'].status, expected);
    if (expected === 'ok') assert.equal(retry.fp['123'].sid, '456');
    else assert.equal(retry.fp['123'].err, 'Product already imported to Shopify');
  }
  const unavailable = setup(async url => url.includes('/lookup?') ? response(503,{}) : response(409,{detail:'Product already imported to Shopify'}));
  await unavailable.importOne('123');
  assert.equal(unavailable.fp['123'].status, 'skip');
  assert.equal(unavailable.fp['123'].err, 'Product already imported to Shopify');
  const conflict = setup(async url => {
    assert(!url.includes('/lookup?'));
    return response(409, {detail:'Supplier link conflict'});
  });
  await conflict.importOne('123');
  assert.equal(conflict.fp['123'].err, 'Supplier link conflict');
  console.log('Import statuses: duplicate clicks, completed retries, real conflicts and lookup failures passed.');
})().catch(error => {console.error(error); process.exitCode=1;});
