with demand as (
  select * from {{ ref('int_recall_inventory_demand') }}
)
select
  campaign_id,
  snapshot_date,
  warehouse_id,
  sku,
  product_category,
  urgency_tier,
  available_now_units,
  inbound_units,
  open_order_units,
  safety_stock_units,
  projected_recall_demand_units,
  available_now_units + inbound_units as projected_available_units,
  open_order_units + projected_recall_demand_units as projected_demand_units,
  greatest(open_order_units + projected_recall_demand_units - (available_now_units + inbound_units), 0) as shortage_units,
  (available_now_units + inbound_units)::numeric / nullif((open_order_units + projected_recall_demand_units) / 30.0, 0) as days_of_cover,
  source_refreshed_at
from demand
