const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
function load(name, context) {
  const start = new RegExp(`(?:async\\s+)?function ${name}\\(`).exec(html).index;
  for (let end = html.indexOf('}', start); end >= 0; end = html.indexOf('}', end + 1)) {
    let script;
    try { script = new vm.Script(html.slice(start, end + 1)); } catch { continue; }
    script.runInContext(context);
    return;
  }
  throw Error(`Cannot parse ${name}`);
}
(async () => {
  const wrap = {style: {}, innerHTML: ''};
  const context = vm.createContext({API: '/api', document: {getElementById: () => wrap},
    fetch: async () => ({ok: true, json: async () => ({count: 2, products: [
      {id: 1, aliexpress_id: '111', replacement_aliexpress_id: '100', is_still_dead: true,
       shopify_product_id: '999', shopify_url: null, shopify_link_status: 'missing'},
      {id: 2, aliexpress_id: '222', replacement_aliexpress_id: '200', is_still_dead: false,
       shopify_product_id: '888', shopify_url: 'https://test.myshopify.com/admin/products/888', shopify_link_status: 'verified'}
    ]})})});
  for (const name of ['escapeHtml', 'shopifyLinkSummary', 'canDeleteDeadListing', 'deadListingDeleteControls', 'deadListingDeleteToolbar', 'loadDeadListings']) load(name, context);
  await context.loadDeadListings();
  assert.match(wrap.innerHTML, /1 product need remapping/);
  assert.match(wrap.innerHTML, /1 product already remapped/);
  assert.match(wrap.innerHTML, /Product not found in the connected Shopify store/);
  assert.match(wrap.innerHTML, /999/);
  assert.doesNotMatch(wrap.innerHTML, /href="[^"]*999/);
  assert.match(wrap.innerHTML, /href="https:\/\/test.myshopify.com\/admin\/products\/888"/);
  assert(!html.includes('https://admin.shopify.com/products/'));
  assert.match(wrap.innerHTML, /Delete local listing/);
  assert.match(wrap.innerHTML, /Select all eligible/);
  assert.equal(context.canDeleteDeadListing({is_still_dead: false, shopify_link_status: 'missing'}, 'imported'), false);
  assert.equal(context.canDeleteDeadListing({is_still_dead: true, shopify_link_status: 'verified'}, 'imported'), false);
  assert.equal(context.canDeleteDeadListing({is_still_dead: true, shopify_link_status: 'unverified'}, 'imported'), false);
  assert.equal(context.canDeleteDeadListing({shopify_link_status: 'missing'}, 'mapping'), true);

  let sent = 0, refreshed = 0;
  context.confirm = () => false;
  context.showToast = () => {};
  context.loadStats = async () => {};
  context.loadDeadListings = async () => { refreshed++; };
  context.fetch = async (url, request) => {
    sent++;
    assert.equal(url, '/api/dead-listings/delete');
    assert.deepEqual(JSON.parse(request.body), {items: [{source: 'imported', id: 1}]});
    return {ok: true, json: async () => ({deleted: [], skipped: [{id: 1, reason: 'Product still exists in Shopify'}]})};
  };
  vm.runInContext('let deadListingDeleteBusy = false;', context);
  load('deleteDeadListingSelection', context);
  await context.deleteDeadListingSelection('imported', 1);
  assert.equal(sent, 0);
  context.confirm = () => true;
  await context.deleteDeadListingSelection('imported', 1);
  assert.equal(sent, 1);
  assert.equal(refreshed, 1);
  assert.match(wrap.innerHTML, /Product still exists in Shopify/);
  console.log('Dead-listing links, missing product state and repeat-remap classification passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
