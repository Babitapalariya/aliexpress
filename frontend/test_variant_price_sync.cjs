const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) new vm.Script(match[1]);

function load(name, context) {
  const match = new RegExp(`(?:async\\s+)?function ${name}\\(`).exec(html);
  assert(match, `Missing function ${name}`);
  for (let end = html.indexOf('}', match.index); end >= 0; end = html.indexOf('}', end + 1)) {
    let script;
    try { script = new vm.Script(html.slice(match.index, end + 1)); } catch { continue; }
    script.runInContext(context);
    return;
  }
  throw Error(`Cannot parse ${name}`);
}

async function checkForm(mapping) {
  const inputs = [2, 5, 8].map((increase, i) => ({
    value: String(10 + increase),
    dataset: {variantId: String(i + 1), originalPrice: '10', priceIncrease: '0', dirty: 'true', editMode: 'increase'},
  }));
  let badge = null;
  const cell = {querySelector: () => badge, insertAdjacentHTML: () => { badge = {remove: () => { badge = null; }}; }};
  const button = {style: {}, innerHTML: '', disabled: false};
  const row = {style: {}, querySelector: selector => {
    if (selector.includes('variant-price-input')) return inputs[1];
    if (selector === 'td') return cell;
    if (selector === '.lock-btn .ti-lock') return button.innerHTML.includes('ti-lock"') ? {} : null;
    return null;
  }};
  button.closest = () => row;
  let locked = false;
  const context = vm.createContext({
    API: '/api', LOCK_TYPE_META: {price: {label: 'Price'}},
    document: {getElementById: () => ({value: '123'}), querySelectorAll: () => inputs, querySelector: () => null},
    fetch: async () => ({ok: true, locked: locked = !locked}),
    safeJson: async response => response,
    showToast: (message, type) => { if (type === 'err') throw Error(message); },
    loadVariantsForModal: () => { throw Error('Lock toggle discarded pending edits'); },
    loadVariantsForMappingModal: () => { throw Error('Lock toggle discarded pending edits'); },
  });
  const collect = mapping ? 'getEditedMappingVariantPrices' : 'getEditedVariantPrices';
  const toggle = mapping ? 'toggleMappingVariantLockType' : 'toggleVariantLockType';
  for (const name of ['updateVariantLockButton', collect, toggle]) load(name, context);
  const original = JSON.stringify(context[collect]());
  await context[toggle](button, 2, 'price');
  assert.equal(inputs[1].dataset.priceLocked, 'true');
  assert.equal(JSON.stringify(context[collect]()), original);
  assert.deepEqual(Array.from(context[collect](), row => row.price_increase), [2, 5, 8]);
  await context[toggle](button, 2, 'price');
  assert.equal(inputs[1].dataset.priceLocked, 'false');
  assert.equal(JSON.stringify(context[collect]()), original);
  // A second manual increase preserves the previously saved markup.
  inputs[0].dataset.priceIncrease = '2';
  inputs[0].dataset.originalPrice = '12';
  inputs[0].value = '15';
  assert.equal(context[collect]()[0].price_increase, 5);
}

(async () => {
  const routing = vm.createContext({});
  load('formatIncrease', routing);
  load('refreshManualIncrease', routing);
  assert.equal(routing.formatIncrease(null), '\u2014');
  assert.equal(routing.formatIncrease(3), '+$3.00');
  assert.equal(routing.formatIncrease(-2), '-$2.00');
  const manualCell = {};
  routing.refreshManualIncrease({value: '17', dataset: {priceIncrease: '3', originalPrice: '15'},
    closest: () => ({querySelector: () => manualCell})});
  assert.equal(manualCell.textContent, '+$5.00');
  load('getApiBase', routing);
  for (const hostname of ['', 'localhost', '127.0.0.1', '[::1]']) {
    assert.equal(routing.getApiBase({hostname}), 'http://127.0.0.1:8001/api');
  }
  assert.equal(routing.getApiBase({hostname: 'aliexpress.retradviews.com'}), 'https://aliexpress.retradviews.com/api');
  await checkForm(false);
  await checkForm(true);
  console.log('Both forms preserve individual increases through lock/unlock; JavaScript syntax passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
