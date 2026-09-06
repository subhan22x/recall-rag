{% test transfer_within_limits(model, column_name) %}
select *
from {{ model }}
where {{ column_name }} > shortage_units
   or {{ column_name }} > transferable_surplus_units
   or {{ column_name }} < 0
{% endtest %}
