select snapshot_date, warehouse_id, sku, on_hand, reserved, damaged, source_refreshed_at
from {{ source('public', 'raw_inventory_snapshots') }}
