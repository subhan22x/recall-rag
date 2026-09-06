select component_pattern, product_category, version, active
from {{ source('public', 'raw_component_product_crosswalk') }}
where active
