select source_warehouse_id, destination_warehouse_id, lead_time_days, active
from {{ source('public', 'raw_warehouse_routes') }}
where active
