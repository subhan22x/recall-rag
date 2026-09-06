select source_key, campaign_id, make, model, model_year, source_refreshed_at
from {{ source('public', 'raw_nhtsa_recall_vehicles') }}
