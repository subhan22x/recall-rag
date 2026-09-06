select purchase_order_id, warehouse_id, sku, quantity, expected_date, status, source_refreshed_at
from {{ source('public', 'raw_purchase_orders') }}
where status = 'open'
