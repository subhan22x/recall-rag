select
  warehouse_id,
  sku,
  sum(quantity)::numeric / 3 as baseline_30d_units,
  max(source_refreshed_at) as source_refreshed_at
from {{ ref('stg_sales_orders') }}
where order_date >= current_date - interval '90 days'
group by 1,2
