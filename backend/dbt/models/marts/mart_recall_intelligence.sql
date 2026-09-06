select
  campaign_id,
  manufacturer,
  make,
  model,
  model_year,
  component,
  urgency_tier,
  park_outside,
  do_not_drive,
  report_date,
  source_refreshed_at
from {{ ref('int_recall_campaign_vehicles') }}
