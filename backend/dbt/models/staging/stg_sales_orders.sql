select order_line_id, order_date, warehouse_id, sku, quantity, source_refreshed_at
from {{ source('public', 'raw_sales_orders') }}
