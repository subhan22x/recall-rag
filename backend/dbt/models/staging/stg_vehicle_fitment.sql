select sku, make, model, model_year
from {{ source('public', 'raw_vehicle_fitment') }}
