select sku, product_name, product_category, unit_cost, active, source_refreshed_at
from {{ source('public', 'raw_product_catalog') }}
where active
