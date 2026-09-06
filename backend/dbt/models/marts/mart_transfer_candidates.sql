with destination as (
  select * from {{ ref('mart_stockout_risk') }} where shortage_units > 0
), source_inventory as (
  select * from {{ ref('int_inventory_position') }}
), source_demand as (
  select warehouse_id, sku, sum(projected_recall_demand_units) as source_recall_demand_units
  from {{ ref('mart_recall_part_demand') }}
  group by 1,2
), routes as (
  select * from {{ ref('stg_warehouse_routes') }}
)
select
  destination.campaign_id,
  destination.sku,
  destination.product_category,
  destination.urgency_tier,
  destination.warehouse_id as destination_warehouse_id,
  source_inventory.warehouse_id as source_warehouse_id,
  routes.lead_time_days as route_lead_time_days,
  destination.shortage_units,
  greatest(
    source_inventory.available_now_units
      - coalesce(source_demand.source_recall_demand_units, 0)
      - source_inventory.safety_stock_units,
    0
  ) as transferable_surplus_units,
  least(
    destination.shortage_units,
    greatest(
      source_inventory.available_now_units
        - coalesce(source_demand.source_recall_demand_units, 0)
        - source_inventory.safety_stock_units,
      0
    )
  ) as recommended_transfer_units,
  destination.source_refreshed_at
from destination
join routes on routes.destination_warehouse_id = destination.warehouse_id
join source_inventory on source_inventory.warehouse_id = routes.source_warehouse_id and source_inventory.sku = destination.sku
left join source_demand on source_demand.warehouse_id = source_inventory.warehouse_id and source_demand.sku = source_inventory.sku
where source_inventory.warehouse_id <> destination.warehouse_id
