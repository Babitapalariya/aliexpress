import copy
import unittest

from app.aliexpress import _parse_product
from app.shopify import normalize_aliexpress_product


class DuplicateSupplierLabelTests(unittest.TestCase):
    def raw(self, warehouses=(201336106,)):
        rows = []
        for warehouse in warehouses:
            for value_id, price in [(10, "44.39"), (1202, "27.29"), (94, "18.62")]:
                rows.append({
                    "sku_id": f"{value_id}-{warehouse}", "sku_price": price,
                    "offer_sale_price": price, "currency_code": "USD", "sku_available_stock": 5,
                    "ae_sku_property_dtos": {"ae_sku_property_d_t_o": [
                        {"sku_property_id": 14, "property_value_id": value_id,
                         "sku_property_name": "Color", "property_value_definition_name": "for DeWalt",
                         "sku_image": f"https://example.com/{value_id}.png"},
                        {"sku_property_id": 560, "property_value_id": 353308,
                         "sku_property_name": "Rated Voltage", "sku_property_value": "12V"},
                        {"sku_property_id": 200007763, "property_value_id": warehouse,
                         "sku_property_name": "Ships From", "sku_property_value":
                         "United States" if warehouse == 201336106 else "China Mainland"},
                    ]},
                })
        return {"aliexpress_ds_product_get_response": {"result": {
            "ae_item_base_info_dto": {"product_id": "3256812460317394"},
            "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": rows}}}}

    def test_same_labels_keep_three_distinct_skus_prices_images_and_stock(self):
        raw = self.raw()
        before = copy.deepcopy(raw)
        result = _parse_product(raw)
        self.assertEqual(result["sku_count"], 3)
        self.assertEqual(result["total_stock"], 15)
        self.assertEqual(result["sale_price"], "18.62")
        self.assertEqual([s["sale_price"] for s in result["skus"]], ["44.39", "27.29", "18.62"])
        self.assertEqual(len({s["image"] for s in result["skus"]}), 3)
        payload = normalize_aliexpress_product(result)
        self.assertEqual(len(payload["variants"]), 3)
        self.assertEqual(len({v["option1"] for v in payload["variants"]}), 3)
        self.assertEqual(raw, before)

    def test_only_warehouse_duplicates_are_collapsed(self):
        result = _parse_product(self.raw((201336106, 201336100)))
        self.assertEqual(result["sku_count"], 3)
        self.assertTrue(all(s["sku_id"].endswith("201336106") for s in result["skus"]))
        self.assertTrue(all(len(s["options"]) == 2 for s in result["skus"]))

    def test_unique_names_stay_unchanged(self):
        raw = self.raw()
        rows = raw["aliexpress_ds_product_get_response"]["result"]["ae_item_sku_info_dtos"]["ae_item_sku_info_d_t_o"]
        for i, row in enumerate(rows):
            row["ae_sku_property_dtos"]["ae_sku_property_d_t_o"][0]["property_value_definition_name"] = f"Pack {i}"
        self.assertEqual([s["label"] for s in _parse_product(raw)["skus"]],
                         [f"Pack {i} / 12V" for i in range(3)])


if __name__ == "__main__":
    unittest.main()
