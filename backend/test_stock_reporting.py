import ast
from pathlib import Path
import unittest


class StockReportingTests(unittest.TestCase):
    def parse(self, stocks):
        path = Path(__file__).parent / 'app' / 'aliexpress.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name == '_parse_inventory']
        ns = {'_resolve_sku_info': lambda sku: {'label': 'A'}}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), ns)
        return ns['_parse_inventory']({}, [{'sku_id': str(i), 'sku_available_stock': stock}
                                          for i, stock in enumerate(stocks)])

    def test_reported_zero_is_not_missing_inventory(self):
        result = self.parse(['0', 0])
        self.assertEqual(result['total_stock'], 0)
        self.assertIs(result['stock_available'], False)
        self.assertEqual(result['stock_source'], 'sku_available_stock')

    def test_unknown_variants_do_not_prove_out_of_stock(self):
        for stocks in ([], [None], [0, None], ['invalid'], [-1]):
            result = self.parse(stocks)
            self.assertIsNone(result['stock_available'])
            self.assertIsNone(result['total_stock'])

    def test_positive_inventory_is_preserved(self):
        result = self.parse([0, '7', 3])
        self.assertEqual(result['total_stock'], 10)
        self.assertIs(result['stock_available'], True)
