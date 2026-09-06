with latest_inventory as (
  select distinct on (warehouse_id, sku)
    snapshot_date, warehouse_id, sku, on_hand, reserved, damaged, source_refreshed_at
  from {{ ref('stg_inventory_snapshots') }}
  order by warehouse_id, sku, snapshot_date desc
), inbound as (
  select warehouse_id, sku, sum(quantity) as inbound_units
  from {{ ref('stg_purchase_orders') }}
  where expected_date <= current_date + interval '30 days'
  group by 1,2
), open_orders as (
  select warehouse_id, sku, sum(quantity) as open_order_units
  from {{ ref('stg_open_sales_orders') }}
  where due_date <= current_date + interval '30 days'
  group by 1,2
), safety as (
  select * from {{ ref('stg_safety_stock_rules') }}
)
select
  inventory.snapshot_date,
  inventory.warehouse_id,
  inventory.sku,
  inventory.on_hand - inventory.reserved - inventory.damaged as available_now_units,
  coalesce(inbound.inbound_units, 0) as inbound_units,
  coalesce(open_orders.open_order_units, 0) as open_order_units,
  coalesce(safety.safety_stock_units, 0) as safety_stock_units,
  inventory.source_refreshed_at
from latest_inventory inventory
left join inbound using (warehouse_id, sku)
left join open_orders using (warehouse_id, sku)
left join safety using (warehouse_id, sku)
