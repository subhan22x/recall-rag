select vehicle_id, warehouse_id, make, model, model_year, active, source_refreshed_at
from {{ source('public', 'raw_customer_fleet') }}
where active
