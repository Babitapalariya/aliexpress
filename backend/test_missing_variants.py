import copy
import unittest
from unittest.mock import Mock, patch
from fastapi import HTTPException
from app import missing_variants as repair


class MissingVariantTests(unittest.TestCase):
    def setUp(self):
        self.product = {"skus": [dict(sku_id=str(i), label=f"Pack {i}", sale_price=str(i * 10), stock=i,
            options=[dict(id="14", name="Pack", value=str(i))]) for i in range(1, 4)]}
        self.state = {11: dict(ae_sku_id="1", price="37.00", price_increase=27,
                              locks={"price": True, "inventory": True, "image": True})}

    def test_existing_manual_price_and_locks_are_never_sent(self):
        plan = repair.plan_missing_variants(self.product, self.state, 5)
        self.assertEqual(plan["variants"][0], {"id": 11, "option1": "1", "option2": None, "option3": None})
        self.assertEqual([v["price"] for v in plan["variants"][1:]], ["25.00", "35.00"])
        self.assertEqual([v["stock"] for v in plan["missing"]], [2, 3])

    def test_missing_or_duplicate_supplier_links_block_all_writes(self):
        for state in ({11: {"ae_sku_id": "unknown"}}, {11: {"ae_sku_id": "1"}, 12: {"ae_sku_id": "1"}}):
            with patch.object(repair.shopify, "get_variant_sync_state", return_value=state), \
                 patch.object(repair.shopify, "_shopify_request") as request:
                with self.assertRaises(HTTPException):
                    repair.restore_missing_variants("123", self.product)
                request.assert_not_called()

    def test_repair_only_sets_new_stock_and_preserves_existing_ids(self):
        after = copy.deepcopy(self.state)
        after.update({12: dict(ae_sku_id="2"), 13: dict(ae_sku_id="3")})
        response = Mock(json=lambda: {"product": {"variants": [{"id": 11}]}})
        with patch.object(repair.shopify, "get_variant_sync_state", side_effect=[self.state, after]), \
             patch.object(repair.shopify, "_shopify_request", return_value=response) as request, \
             patch.object(repair.shopify, "_h", return_value={}), \
             patch.object(repair.shopify, "set_variant_inventory_quantities") as stock, \
             patch.object(repair.shopify, "backfill_sku_images", return_value={"remaining": 0}):
            result = repair.restore_missing_variants("123", self.product)
        self.assertEqual(result["added"], 2)
        stock.assert_called_once_with("123", {12: 2, 13: 3})
        self.assertFalse(request.call_args.kwargs["retry_ambiguous"])

    def test_repeat_repair_does_not_create_duplicates(self):
        state = {i: {"ae_sku_id": str(i)} for i in range(1, 4)}
        with patch.object(repair.shopify, "get_variant_sync_state", return_value=state), \
             patch.object(repair.shopify, "_shopify_request") as request:
            self.assertEqual(repair.restore_missing_variants("123", self.product)["added"], 0)
            request.assert_not_called()

    def test_concurrent_variant_change_stops_before_write(self):
        response = Mock(json=lambda: {"product": {"variants": [{"id": 11}, {"id": 99}]}})
        with patch.object(repair.shopify, "get_variant_sync_state", return_value=self.state), \
             patch.object(repair.shopify, "_h", return_value={}), \
             patch.object(repair.shopify, "_shopify_request", return_value=response) as request:
            with self.assertRaises(HTTPException):
                repair.restore_missing_variants("123", self.product)
            self.assertEqual(request.call_count, 1)

    def test_unknown_stock_stops_before_create(self):
        self.product["skus"][1]["stock"] = None
        with self.assertRaises(HTTPException):
            repair.plan_missing_variants(self.product, self.state)


if __name__ == "__main__":
    unittest.main()
