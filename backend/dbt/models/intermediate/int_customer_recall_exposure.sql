with fitment as (
  select * from {{ ref('int_recall_product_fitment') }}
), fleet as (
  select * from {{ ref('stg_customer_fleet') }}
), branch_totals as (
  select warehouse_id, count(distinct vehicle_id) as total_branch_fleet_vehicles
  from fleet
  group by 1
)
select
  fitment.campaign_id,
  fitment.sku,
  fitment.product_category,
  fitment.urgency_tier,
  fitment.crosswalk_version,
  fleet.warehouse_id,
  count(distinct fleet.vehicle_id) as matching_customer_vehicles,
  max(branch_totals.total_branch_fleet_vehicles) as total_branch_fleet_vehicles
from fitment
join fleet on fleet.make = fitment.make and fleet.model = fitment.model and fleet.model_year = fitment.model_year
join branch_totals using (warehouse_id)
group by 1,2,3,4,5,6
