with recall_vehicles as (
  select * from {{ ref('int_recall_campaign_vehicles') }}
), crosswalk as (
  select * from {{ ref('stg_component_product_crosswalk') }}
), fitment as (
  select * from {{ ref('stg_vehicle_fitment') }}
), catalog as (
  select * from {{ ref('stg_product_catalog') }}
)
select
  rv.campaign_id,
  rv.component,
  rv.urgency_tier,
  rv.make,
  rv.model,
  rv.model_year,
  catalog.sku,
  catalog.product_category,
  crosswalk.version as crosswalk_version
from recall_vehicles rv
join crosswalk on upper(rv.component) like '%' || upper(crosswalk.component_pattern) || '%'
join catalog on catalog.product_category = crosswalk.product_category
join fitment on fitment.sku = catalog.sku
  and fitment.make = rv.make and fitment.model = rv.model and fitment.model_year = rv.model_year
