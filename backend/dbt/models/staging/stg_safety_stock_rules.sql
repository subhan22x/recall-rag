select warehouse_id, sku, safety_stock_units
from {{ source('public', 'raw_safety_stock_rules') }}
