"""Create complete products beyond the REST API's 100-variant limit."""
from fastapi import HTTPException
from . import shopify
from .remap_variants import _sync_replacement_inventory


def create_large_product(product, normalized):
    skus = shopify.resolve_supplier_images(product['skus'])
    options = normalized['options']
    stocks, variants = {}, []
    files = {image['src']: {'originalSource': image['src'], 'contentType': 'IMAGE'}
             for image in normalized.get('images', [])}
    for sku, source in zip(skus, normalized['variants']):
        sid = str(sku.get('sku_id') or '')
        if not sid or sid in stocks:
            raise HTTPException(400, 'Every supplier variant must have a unique SKU ID')
        stocks[sid] = source['inventory_quantity']
        variant = {
            'price': source['price'], 'inventoryItem': {'tracked': True},
            'inventoryPolicy': 'DENY', 'metafields': source['metafields'],
            'optionValues': [{'optionName': option['name'], 'name': source[f'option{i}']}
                             for i, option in enumerate(options, 1)],
        }
        if sku.get('image'):
            file = files.setdefault(sku['image'], {'originalSource': sku['image'], 'contentType': 'IMAGE'})
            variant['file'] = file
        variants.append(variant)
    response = shopify._shopify_request('GET', f'{shopify._base()}/locations.json',
                                       headers=shopify._h(), timeout=20)
    response.raise_for_status()
    locations = [loc for loc in response.json().get('locations', [])
                 if loc.get('active', True) and not loc.get('legacy', False)]
    if not locations:
        raise HTTPException(409, 'No active Shopify stock location; import stopped')
    payload = {
        'title': normalized['title'], 'descriptionHtml': normalized['body_html'],
        'vendor': normalized['vendor'], 'productType': normalized['product_type'],
        'status': 'DRAFT', 'tags': normalized['tags'].split(', '),
        'metafields': [dict(field, value=str(field['value'])) for field in normalized['metafields']],
        'productOptions': [{'name': option['name'], 'position': i,
                            'values': [{'name': value} for value in option['values']]}
                           for i, option in enumerate(options, 1)],
        'variants': variants,
    }
    if files:
        payload['files'] = list(files.values())
    query = '''mutation ImportLargeProduct($input: ProductSetInput!) {
      productSet(input: $input, synchronous: true) {
        product { id title status } userErrors { field message }
      }
    }'''
    response = shopify._shopify_request('POST', shopify._graphql_url(),
        json={'query': query, 'variables': {'input': payload}}, headers=shopify._h(),
        timeout=120, retry_ambiguous=False)
    response.raise_for_status()
    body = response.json()
    result = (body.get('data') or {}).get('productSet') or {}
    errors = body.get('errors') or result.get('userErrors')
    saved = result.get('product') or {}
    if errors or not saved.get('id'):
        raise HTTPException(502, f"Shopify large product import failed: {errors or 'missing product confirmation'}")
    product_id = saved['id'].rsplit('/', 1)[-1]
    warnings = []
    try:
        _sync_replacement_inventory(product_id, stocks, locations[0]['id'])
    except Exception as exc:
        # Creation already succeeded: return its ID so callers persist the link.
        # Retrying creation here would leave an orphan or create a duplicate.
        warning = 'Product created, but stock sync is incomplete. Use Pull live stock from AliExpress: ' + str(getattr(exc, 'detail', exc))
        warnings.append(warning)
        shopify.logger.warning('Shopify product %s: %s', product_id, warning)
    return {'product': {'id': product_id, 'title': saved.get('title', normalized['title']),
                        'status': saved.get('status', 'DRAFT').lower()}, 'warnings': warnings}
