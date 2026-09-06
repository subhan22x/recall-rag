with recalls as (
  select * from {{ ref('stg_nhtsa_recalls') }}
), vehicles as (
  select * from {{ ref('stg_recall_vehicles') }}
)
select
  recalls.campaign_id,
  recalls.manufacturer,
  recalls.component,
  recalls.summary,
  recalls.consequence,
  recalls.remedy,
  recalls.park_outside,
  recalls.do_not_drive,
  recalls.report_date,
  vehicles.make,
  vehicles.model,
  vehicles.model_year,
  greatest(recalls.source_refreshed_at, vehicles.source_refreshed_at) as source_refreshed_at,
  case
    when recalls.do_not_drive or recalls.park_outside then 'critical'
    when upper(recalls.component) like any (array['%BRAKE%', '%STEERING%', '%AIR BAG%', '%FUEL%', '%FIRE%']) then 'high'
    else 'standard'
  end as urgency_tier
from recalls
join vehicles using (source_key, campaign_id)
