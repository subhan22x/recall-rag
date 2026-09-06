select
  demand.campaign_id,
  demand.warehouse_id,
  demand.sku,
  demand.product_category,
  demand.urgency_tier,
  demand.projected_recall_demand_units,
  position.snapshot_date,
  position.available_now_units,
  position.inbound_units,
  position.open_order_units,
  position.safety_stock_units,
  position.source_refreshed_at
from {{ ref('mart_recall_part_demand') }} demand
join {{ ref('int_inventory_position') }} position using (warehouse_id, sku)
