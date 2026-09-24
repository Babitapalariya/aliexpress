import ast
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from app import shopify


def resolver():
    tree = ast.parse(Path(__file__).with_name('app').joinpath('aliexpress.py').read_text(encoding='utf-8'))
    ns = {}
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_resolve_sku_info']
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'aliexpress.py', 'exec'), ns)
    return ns['_resolve_sku_info']


class OptionImportTests(unittest.TestCase):
    def setUp(self):
        self.skus = []
        for i, (color, size) in enumerate([('AB0102', 'M'), ('AB0102', 'L'), ('Black', 'M')], 1):
            props = [dict(sku_property_id=14, sku_property_name='Color', property_value_definition_name=color),
                     dict(sku_property_id=5, sku_property_name='Size', property_value_definition_name=size)]
            info = resolver()({'ae_sku_property_dtos': {'ae_sku_property_d_t_o': props}})
            self.skus.append(dict(info, sku_id=str(i), sale_price=str(50+i), stock=i))

    def test_color_size_preserve_real_combinations_prices_and_sku_links(self):
        product = shopify.normalize_aliexpress_product({'skus': self.skus})
        self.assertEqual([o['name'] for o in product['options']], ['Color', 'Size'])
        self.assertEqual([(v['option1'], v['option2']) for v in product['variants']],
                         [('AB0102', 'M'), ('AB0102', 'L'), ('Black', 'M')])
        self.assertEqual([v['price'] for v in product['variants']], ['51', '52', '53'])
        self.assertEqual([v['metafields'][0]['value'] for v in product['variants']], ['1', '2', '3'])
        self.assertEqual([v['inventory_quantity'] for v in product['variants']], [1, 2, 3])

    def test_reordered_properties_use_property_identity(self):
        self.skus[1]['options'].reverse()
        _, rows = shopify.build_supplier_options(self.skus)
        self.assertEqual(rows[1], ['AB0102', 'L'])

    def test_incomplete_duplicate_and_excess_options_rejected(self):
        for kind in ('missing', 'duplicate', 'excess'):
            skus = copy.deepcopy(self.skus)
            if kind == 'missing': skus[1]['options'].pop()
            if kind == 'duplicate': skus[1]['options'] = copy.deepcopy(skus[0]['options'])
            if kind == 'excess':
                for s in skus:
                    s['options'] += [dict(id='a', name='A', value='a'), dict(id='b', name='B', value='b')]
            with self.subTest(kind=kind), self.assertRaises(HTTPException):
                shopify.build_supplier_options(skus)

    def test_legacy_labels_remain_readable(self):
        options, rows = shopify.build_supplier_options([{'label': 'Red / Large'}])
        self.assertEqual(options[0]['name'], 'Variant')
        self.assertEqual(rows, [['Red / Large']])

    def test_warehouse_collapse_keeps_color_and_size_options(self):
        tree = ast.parse(Path(__file__).with_name('app').joinpath('aliexpress.py').read_text(encoding='utf-8'))
        ns = {'COUNTRY_CODE_TO_NAME': {'US': 'United States'},
              'KNOWN_SHIP_LOCATIONS': {'united states', 'china mainland'}}
        selected = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name in ('_sku_stock_int', '_filter_skus_by_ship_location')]
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'aliexpress.py', 'exec'), ns)
        skus = []
        for country in ('United States', 'China Mainland'):
            s = copy.deepcopy(self.skus[0])
            s['label'] += ' / ' + country
            s['options'].append(dict(id='200007763', name='Ships From', value=country))
            skus.append(s)
        result = ns['_filter_skus_by_ship_location'](skus, 'US')
        self.assertEqual(len(result), 1)
        self.assertEqual([o['name'] for o in result[0]['options']], ['Color', 'Size'])
        self.assertEqual(len(skus[0]['options']), 3)

    def test_repair_response_with_replaced_ids_is_not_success(self):
        state = {i: {'ae_sku_id': str(i), 'label': s['label']} for i, s in enumerate(self.skus, 1)}
        with patch.object(shopify, 'get_variant_sync_state', return_value=state), \
             patch.object(shopify, '_h', return_value={}), \
             patch.object(shopify.requests, 'put', return_value=Mock(json=lambda: {'product': {'variants': []}})):
            with self.assertRaises(HTTPException) as error:
                shopify.repair_supplier_option_groups('123', self.skus)
            self.assertEqual(error.exception.status_code, 502)

    def test_repair_preserves_variant_ids_and_omits_price_stock_and_locks(self):
        state = {i: {'ae_sku_id': str(i), 'label': s['label']} for i, s in enumerate(self.skus, 1)}
        def write(*args, **kwargs):
            product = kwargs['json']['product']
            self.assertEqual(set(product), {'id', 'options', 'variants'})
            for v in product['variants']:
                self.assertEqual(set(v), {'id', 'option1', 'option2', 'option3'})
            return Mock(json=lambda: {'product': product})
        with patch.object(shopify, 'get_variant_sync_state', return_value=state), \
             patch.object(shopify, '_h', return_value={}), \
             patch.object(shopify.requests, 'put', side_effect=write):
            result = shopify.repair_supplier_option_groups('123', self.skus)
        self.assertEqual(result['updated'], 3)

    def test_unmatched_repair_does_not_write(self):
        with patch.object(shopify, 'get_variant_sync_state', return_value={8: {'ae_sku_id': 'unknown', 'label': 'Unknown'}}), \
             patch.object(shopify.requests, 'put') as write:
            with self.assertRaises(HTTPException):
                shopify.repair_supplier_option_groups('123', self.skus)
            write.assert_not_called()

    def test_images_shared_by_color_without_crossing_to_other_colors(self):
        self.skus[0]['image'] = 'https://example.com/red.jpg'
        self.skus[0]['options'][0]['image'] = self.skus[0]['image']
        rows = shopify.resolve_supplier_images(self.skus)
        self.assertEqual(rows[1]['image'], rows[0]['image'])
        self.assertIsNone(rows[2]['image'])
        self.assertIsNone(self.skus[1]['image'])

    def test_image_parser_keeps_property_identity_and_normalizes_url(self):
        info = resolver()({'ae_sku_property_dtos': [dict(
            sku_property_id=14, sku_property_name='Color', property_value_definition_name='Red',
            sku_image='//example.com/red.jpg')]})
        self.assertEqual(info['image'], 'https://example.com/red.jpg')
        self.assertEqual(info['options'][0]['image'], info['image'])

    def test_image_sync_matches_reordered_sizes_and_reports_missing(self):
        self.skus[0]['image'] = 'https://example.com/red.jpg'
        self.skus[0]['options'][0]['image'] = self.skus[0]['image']
        variants = [dict(id=i, aliexpress_sku_id=str(i), label=s['label'], image_id=None)
                    for i, s in enumerate(self.skus, 1)]
        with patch.object(shopify, '_get_all_variants_for_image_sync', return_value=list(reversed(variants))), \
             patch.object(shopify, 'get_locked_variant_ids', return_value=set()), \
             patch.object(shopify, '_upload_image_to_shopify', return_value=99) as upload, \
             patch.object(shopify, '_h', return_value={}), \
             patch.object(shopify, '_shopify_request', return_value=Mock()) as write:
            result = shopify.backfill_sku_images('123', self.skus)
        self.assertEqual(result['attached'], 2)
        self.assertEqual(result['remaining'], 1)
        upload.assert_called_once()
        self.assertEqual([c.kwargs['json']['variant'] for c in write.call_args_list],
                         [{'id': 2, 'image_id': 99}, {'id': 1, 'image_id': 99}])

    def test_image_sync_preserves_locks_existing_images_and_unknown_skus(self):
        variants = [dict(id=i, aliexpress_sku_id=str(i), image_id=99 if i == 1 else None)
                    for i in (1, 2, 8)]
        with patch.object(shopify, '_get_all_variants_for_image_sync', return_value=variants), \
             patch.object(shopify, 'get_locked_variant_ids', return_value={2}), \
             patch.object(shopify, '_upload_image_to_shopify') as upload:
            result = shopify.backfill_sku_images('123', self.skus)
        upload.assert_not_called()
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(result['remaining'], 1)

    def test_upload_retries_rate_limit(self):
        throttled = Mock(status_code=429, headers={'Retry-After': '0.5'})
        success = Mock(status_code=200, json=lambda: {'image': {'id': 99}})
        with patch.object(shopify.requests, 'request', side_effect=[throttled, success]) as request, \
             patch.object(shopify, '_throttle'), patch.object(shopify.time, 'sleep'), \
             patch.object(shopify, '_h', return_value={}):
            self.assertEqual(shopify._upload_image_to_shopify('123', 'https://example.com/red.jpg'), 99)
        self.assertEqual(request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
