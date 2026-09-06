with exposure as (
  select * from {{ ref('int_customer_recall_exposure') }}
), baseline as (
  select * from {{ ref('int_recall_baseline_sales') }}
)
select
  exposure.campaign_id,
  exposure.warehouse_id,
  exposure.sku,
  exposure.product_category,
  exposure.urgency_tier,
  exposure.crosswalk_version,
  exposure.matching_customer_vehicles,
  exposure.total_branch_fleet_vehicles,
  exposure.matching_customer_vehicles::numeric / nullif(exposure.total_branch_fleet_vehicles, 0) as recall_exposure_factor,
  coalesce(baseline.baseline_30d_units, 0) as baseline_30d_units,
  case exposure.urgency_tier when 'critical' then 1.50 when 'high' then 1.25 else 1.10 end as urgency_multiplier,
  coalesce(baseline.baseline_30d_units, 0)
    * case exposure.urgency_tier when 'critical' then 1.50 when 'high' then 1.25 else 1.10 end
    * (1 + exposure.matching_customer_vehicles::numeric / nullif(exposure.total_branch_fleet_vehicles, 0)) as projected_recall_demand_units,
  baseline.source_refreshed_at
from exposure
left join baseline using (warehouse_id, sku)
