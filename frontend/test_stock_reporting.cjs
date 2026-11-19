const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(`${__dirname}/index.html`, 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
(async () => {
  for (const [name, prefix] of [['refreshProductDetails', 'modal'], ['refreshMappingDetails', 'mm']]) {
    const start = script.indexOf(`async function ${name}()`);
    const end = script.indexOf('\n}', start) + 2;
    // Product function is indented; use its next top-level function boundary.
    const code = name === 'refreshProductDetails'
      ? script.slice(start, script.indexOf('\n  }', start) + 4)
      : script.slice(start, end);
    for (const [data, expected] of [
      [{stock_available:false, total_stock:0}, 'AliExpress API reports 0 stock'],
      [{stock_available:null, total_stock:null}, 'Stock not reported'],
      [{stock_available:true, total_stock:7}, '7 units total'],
      [{fetch_error:'Token expired'}, 'Could not fetch live AliExpress stock: Token expired'],
    ]) {
      const elements = {};
      const context = vm.createContext({API:'/api', document:{getElementById(id) {
        return elements[id] ||= {value:'143', innerHTML:''};
      }}, fetch:async () => ({ok:true, json:async () => data}), escapeHtml:s=>s});
      vm.runInContext(code, context);
      await context[name]();
      assert.ok(elements[`${prefix}-inventory-info`].innerHTML.includes(expected), name + ': ' + expected);
    }
  }
  console.log('Stock reporting checks passed for both edit forms');
})();
