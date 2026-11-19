"""Large imports must create all supplier variants without REST product writes."""
import contextlib
import unittest
from unittest.mock import Mock, patch
import requests
from fastapi import HTTPException
from app import shopify, large_product_import


class LargeProductImportTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(shopify.settings, 'SHOPIFY_STORE', 'test'))
        stack.enter_context(patch.object(shopify, '_h', return_value={}))
        stack.enter_context(patch.object(shopify, 'check_product_exists_in_shopify', return_value=False))
        self.inventory = stack.enter_context(patch.object(large_product_import, '_sync_replacement_inventory'))
        self.request = stack.enter_context(patch.object(shopify, '_shopify_request'))
        self.locations = Mock(json=lambda: {'locations': [{'id': 7, 'active': True}]})
        self.created = Mock(json=lambda: {'data': {'productSet': {
            'product': {'id': 'gid://shopify/Product/42', 'title': 'Helmet', 'status': 'DRAFT'},
            'userErrors': []}}})
        self.request.side_effect = [self.locations, self.created]
        self.product = {'product_id': 3256810291770430, 'title': 'Helmet',
            'main_image': 'https://example.com/main.jpg', 'skus': [
                {'sku_id': str(i), 'sale_price': '202.29', 'stock': 0 if i < 76 else 7,
                 'image': f'https://example.com/style-{i // 7}.jpg',
                 'options': [{'id': '14', 'name': 'Color', 'value': f'Style {i // 7}'},
                             {'id': '5', 'name': 'Size', 'value': f'Size {i % 7}'}]}
                for i in range(161)]}

    def test_161_variants_preserve_options_prices_sku_links_images_and_zero_stock(self):
        result = shopify.create_shopify_product(self.product)
        self.assertEqual(result['product']['id'], '42')
        self.assertEqual(result['product']['status'], 'draft')
        call = self.request.call_args
        self.assertTrue(call.args[1].endswith('/graphql.json'))
        self.assertFalse(call.kwargs['retry_ambiguous'])
        payload = call.kwargs['json']['variables']['input']
        self.assertEqual(payload['status'], 'DRAFT')
        self.assertEqual(payload['metafields'][0]['value'], '3256810291770430')
        self.assertEqual([o['name'] for o in payload['productOptions']], ['Color', 'Size'])
        self.assertEqual(len(payload['variants']), 161)
        self.assertEqual(len(payload['files']), 24)
        for i, variant in enumerate(payload['variants']):
            self.assertEqual(variant['metafields'][0]['value'], str(i))
            self.assertEqual(variant['price'], '202.29')
            self.assertNotIn('inventoryQuantities', variant)
            self.assertIn(variant['file'], payload['files'])
        self.inventory.assert_called_once_with('42', {str(i): 0 if i < 76 else 7 for i in range(161)}, 7)

    def test_rejection_and_missing_confirmation_never_sync_inventory(self):
        for body in ({'errors': [{'message': 'Rejected'}]},
                     {'data': {'productSet': {'userErrors': [{'message': 'Invalid variant'}]}}},
                     {'data': {'productSet': {}}}):
            self.request.side_effect = [self.locations, Mock(json=lambda: body)]
            with self.assertRaises(HTTPException):
                shopify.create_shopify_product(self.product)
            self.inventory.assert_not_called()

    def test_timeout_does_not_fall_back_to_rest_or_repeat_creation(self):
        self.request.side_effect = [self.locations, requests.Timeout('Timed out')]
        with self.assertRaises(HTTPException):
            shopify.create_shopify_product(self.product)
        self.assertEqual(self.request.call_count, 2)
        self.inventory.assert_not_called()

    def test_stock_failure_keeps_created_id_and_reports_warning(self):
        self.inventory.side_effect = RuntimeError('Inventory rejected')
        result = shopify.create_shopify_product(self.product)
        self.assertEqual(result['product']['id'], '42')
        self.assertIn('Inventory rejected', result['warnings'][0])
        self.assertEqual(self.request.call_count, 2)

    def test_duplicate_skus_and_missing_locations_stop_before_creation(self):
        self.product['skus'][1]['sku_id'] = '0'
        with self.assertRaises(HTTPException):
            shopify.create_shopify_product(self.product)
        self.request.assert_not_called()
        self.product['skus'][1]['sku_id'] = '1'
        self.request.side_effect = [Mock(json=lambda: {'locations': []})]
        with self.assertRaises(HTTPException):
            shopify.create_shopify_product(self.product)
        self.assertEqual(self.request.call_count, 1)
