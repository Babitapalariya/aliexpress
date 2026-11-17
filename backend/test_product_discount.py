import unittest
from app.aliexpress import _parse_product

class ProductDiscountTests(unittest.TestCase):
    def parse(self, skus):
        return _parse_product({"aliexpress_ds_product_get_response":{"result":{
            "ae_item_base_info_dto":{"product_id":"test","currency_code":"CNY"},
            "ae_item_sku_info_dtos":{"ae_item_sku_info_d_t_o":skus}}}})
    def sku(self, ident, regular, offer=None, currency="USD"):
        return {"sku_id":ident,"sku_price":regular,"offer_sale_price":offer,"currency_code":currency,"sku_available_stock":2}
    def test_discount_and_currency(self):
        p=self.parse([self.sku("1","1739.55","817.59")])
        self.assertEqual((p['original_price'],p['sale_price'],p['currency'],p['discount']),('1739.55','817.59','USD','53.00%'))
        self.assertEqual(p['skus'][0]['discount'],'53.00%')
    def test_no_offer_and_equal_price(self):
        for offer in (None,'10'):
            p=self.parse([self.sku('1','10',offer)])
            self.assertEqual(p['sale_price'],'10');self.assertEqual(p['discount'],'0.00%')
    def test_variant_pair_is_not_mixed(self):
        p=self.parse([self.sku('1','100','90'),self.sku('2','200','80')])
        self.assertEqual((p['original_price'],p['sale_price'],p['discount']),('200','80','60.00%'))
    def test_invalid_and_zero(self):
        for value in ('NaN','Infinity','bad','-1',None):
            p=self.parse([self.sku('1',value)])
            self.assertIsNone(p['sale_price']);self.assertIsNone(p['discount'])
        p=self.parse([self.sku('1','10','0')]);self.assertEqual(p['discount'],'100.00%')
        p=self.parse([self.sku('1','0','0')]);self.assertIsNone(p['discount'])
    def test_mixed_currencies_do_not_compare(self):
        p=self.parse([self.sku('1','10','5','USD'),self.sku('2','20','10','EUR')])
        self.assertIsNone(p['sale_price'])

if __name__=='__main__':unittest.main()
