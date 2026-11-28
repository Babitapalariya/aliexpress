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
  for (const name of ['escapeHtml', 'shopifyLinkSummary', 'loadDeadListings']) load(name, context);
  await context.loadDeadListings();
  assert.match(wrap.innerHTML, /1 product need remapping/);
  assert.match(wrap.innerHTML, /1 product already remapped/);
  assert.match(wrap.innerHTML, /Product not found in the connected Shopify store/);
  assert.match(wrap.innerHTML, /999/);
  assert.doesNotMatch(wrap.innerHTML, /href="[^"]*999/);
  assert.match(wrap.innerHTML, /href="https:\/\/test.myshopify.com\/admin\/products\/888"/);
  assert(!html.includes('https://admin.shopify.com/products/'));
  console.log('Dead-listing links, missing product state and repeat-remap classification passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
