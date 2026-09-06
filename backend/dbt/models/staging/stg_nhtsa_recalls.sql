select
  source_key,
  campaign_id,
  manufacturer,
  component,
  summary,
  consequence,
  remedy,
  park_outside,
  do_not_drive,
  report_date,
  source_url,
  source_refreshed_at
from {{ source('public', 'raw_nhtsa_recalls') }}
