select order_line_id, warehouse_id, sku, quantity, due_date, source_refreshed_at
from {{ source('public', 'raw_open_sales_orders') }}
