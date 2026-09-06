# ELT, dbt, Semantic Layer, and Governed Text-to-SQL

## Why this is separate from RAG

RAG answers: “What does the recall notice say?”

ELT and dbt answer: “How many Dallas customer vehicles are exposed, which compatible parts are needed, and how much inventory is available after incoming supply?”

Those questions need pre-defined joins, calculations, and tests. The model should not invent them in a prompt. dbt implements that business logic in reviewed SQL models; the assistant only asks for approved measurements from those models.

## Demo data and source contract

The project combines public NHTSA records with a **synthetic distributor dataset**, labelled as such in the UI and API. The synthetic dataset is designed for six warehouses (`DFW-01`, `HOU-01`, `AUS-01`, `SAT-01`, `MEM-02`, `PHX-01`) and should contain approximately:

- 300 parts/SKUs and 2,000 vehicle-fitment records.
- 500 fictional customer-fleet vehicles.
- 180 days of order history.
- 90 days of inventory snapshots.
- Purchase orders, warehouse routes, and safety-stock rules.

Raw data lands unchanged, with source/run timestamps. No raw table is queried by the assistant.

```text
raw_nhtsa_recalls             raw_product_catalog
raw_nhtsa_recall_vehicles     raw_vehicle_fitment
raw_customer_fleet            raw_sales_orders
raw_inventory_snapshots       raw_purchase_orders
raw_warehouse_transfers       raw_warehouse_routes
raw_safety_stock_rules        raw_component_product_crosswalk
```

## Common dbt conventions

- `stg_` models clean one source and standardize names/types.
- `int_` models perform reusable joins and business calculations.
- `mart_` models expose a small, stable analytical surface to the semantic layer.
- A dbt source freshness check reports stale raw data.
- Every model has a description and owner; every key join has `not_null`/`unique`/`relationships` tests where applicable.
- Synthetic-data generators are separate from dbt. dbt transforms data; it does not fabricate it.

## Pipeline 1: Recall and vehicle impact intelligence

### Decision it supports

Operations or safety staff need a trusted list of new, serious recall campaigns and the vehicles that they affect.

### Lineage

```text
NHTSA recall API
  -> raw_nhtsa_recalls / raw_nhtsa_recall_vehicles
  -> stg_nhtsa_recalls / stg_recall_vehicles
  -> int_recall_campaign_vehicles
  -> mart_recall_intelligence
```

### Key transformations

- Deduplicate to stable `campaign_id` + vehicle context.
- Normalize manufacturer, make/model, component, report date, and advisory booleans.
- Assign an explainable `urgency_tier`:
  - `critical`: `do_not_drive` or `park_outside` is true.
  - `high`: component/narrative includes brake, steering, airbag, fuel, or fire risk.
  - `standard`: all other campaigns.
- Keep the component and vehicle fitment separate so a campaign can affect several vehicles without duplicating campaign-level counts.

### Mart grain and columns

`mart_recall_intelligence` is one row per campaign + vehicle configuration. It includes `campaign_id`, vehicle attributes, component, advisory flags, `urgency_tier`, report date, source freshness, and a link to the RAG document ID.

### Key dbt tests

- `campaign_id` is not null.
- `urgency_tier` is one of `critical`, `high`, `standard`.
- advisory values are booleans.
- campaign/vehicle grain is unique.
- every row has a known raw source timestamp.

## Pipeline 2: Recall-to-parts demand opportunity

### Decision it supports

Merchandising or operations needs an explainable list of recall-affected parts and branches where demand may rise. This is a scenario estimate—not a machine-learning forecast and not a claim about future sales.

### Lineage

```text
Pipeline 1 mart + product catalog + vehicle fitment + customer fleet + sales orders
  -> stg_product_catalog / stg_vehicle_fitment / stg_customer_fleet / stg_sales_orders
  -> int_recall_product_fitment
  -> int_customer_recall_exposure
  -> int_recall_baseline_sales
  -> mart_recall_part_demand
```

### Key transformations

1. A versioned `raw_component_product_crosswalk` maps an NHTSA component to a compatible product category. It is data, not a hidden model rule, so a reviewer can see and revise it.
2. `int_recall_product_fitment` joins affected vehicles to compatible SKUs.
3. `int_customer_recall_exposure` counts fictional customer-fleet vehicles that match each campaign/branch/part combination.
4. `int_recall_baseline_sales` calculates a simple baseline from the trailing 90 days:

   ```text
   baseline_30d = trailing_90d_units / 3
   recall_exposure_factor = matching_customer_vehicles / total_branch_fleet_vehicles
   urgency_multiplier = 1.50 (critical), 1.25 (high), 1.10 (standard)
   projected_recall_demand = baseline_30d
                             * urgency_multiplier
                             * (1 + recall_exposure_factor)
   ```

5. `mart_recall_part_demand` stores the inputs and formula output so the UI can explain where the estimate came from.

### Mart grain and columns

One row per `campaign_id`, `sku`, and destination warehouse/branch. Include baseline sales, matching fleet count, exposure factor, urgency multiplier, projected 30-day demand, crosswalk version, and data freshness.

### Key dbt tests

- SKU exists in product catalog.
- vehicle-fitment relationship is valid.
- exposure factor is between 0 and 1.
- urgency multiplier is one of the documented values.
- projected demand is non-negative.
- crosswalk mappings are non-null for included demand rows.

## Pipeline 3: Warehouse stockout and transfer risk

### Decision it supports

Warehouse operations needs to see which recall-sensitive SKUs may fall below coverage and whether another location has safe surplus. The output is a recommendation candidate, not an inventory transaction.

### Lineage

```text
Pipeline 2 mart + inventory + open sales orders + purchase orders
+ routes + safety-stock rules
  -> stg_inventory_snapshots / stg_purchase_orders / stg_open_sales_orders
  -> int_inventory_position
  -> int_recall_inventory_demand
  -> int_transferable_inventory
  -> int_transfer_candidates
  -> mart_stockout_risk / mart_transfer_candidates
```

### Key transformations

```text
available_now = on_hand - reserved - damaged
projected_available = available_now + inbound_quantity_due_before_need_date
projected_demand = open_order_demand + projected_recall_demand
shortage = max(projected_demand - projected_available, 0)
days_of_cover = projected_available / projected_daily_demand
transferable_surplus = max(
  source_available_now - source_projected_demand - source_safety_stock,
  0
)
recommended_transfer_qty = min(destination_shortage, source_transferable_surplus)
```

`int_transfer_candidates` joins only permitted routes and ranks candidates by recommended quantity, route lead time, and source coverage after the proposed transfer. A candidate is suppressed if the source would fall below its safety stock.

### Mart grain and columns

- `mart_stockout_risk`: one row per snapshot date, destination warehouse, and SKU.
- `mart_transfer_candidates`: one row per destination shortage, source warehouse, and SKU.

Both marts contain enough operands to reproduce the calculation: stock terms, demand terms, safety stock, arrival date, shortage, source surplus, recommended quantity, and route ID.

### Key dbt tests

- inventory positions and quantities are non-negative after the documented clamps.
- source/destination warehouse IDs exist.
- every recommendation uses an active route.
- recommended transfer is not greater than either shortage or transferable surplus.
- a source with negative post-transfer safety stock cannot produce a candidate.

## Semantic layer

The semantic layer is a YAML contract mapping business language to *already-built* marts. It tells the assistant which metrics and dimensions are allowed; it does not contain a free-form SQL prompt.

Example shape:

```yaml
version: 1
models:
  stockout_risk:
    relation: analytics.mart_stockout_risk
    grain: snapshot_date, warehouse_id, sku
    metrics:
      shortage_units:
        expression: sum(shortage_units)
        description: Units by which projected demand exceeds projected availability.
      at_risk_skus:
        expression: count(distinct case when shortage_units > 0 then sku end)
    dimensions:
      - warehouse_id
      - sku
      - component_category
      - urgency_tier
      - snapshot_date
    joins: []
policies:
  read_only: true
  max_rows: 100
  allowed_models: [recall_intelligence, recall_part_demand, stockout_risk, transfer_candidates]
```

It must be versioned in the repository and loaded by the backend rather than duplicated in Python constants.

## Governed text-to-SQL flow

The model is not allowed to emit executable SQL directly.

1. The model translates a question into a typed `SemanticQuery`, for example:

   ```json
   {
     "model": "stockout_risk",
     "metrics": ["shortage_units", "at_risk_skus"],
     "dimensions": ["warehouse_id"],
     "filters": [{"field": "urgency_tier", "operator": "=", "value": "critical"}],
     "order_by": [{"field": "shortage_units", "direction": "desc"}],
     "limit": 25
   }
   ```

2. Pydantic validates model names, metric names, dimension names, values, operators, and a maximum limit.
3. A deterministic compiler resolves each name through the YAML contract and builds a parameterized `SELECT` against an approved mart.
4. `sqlglot` parses the result and rejects anything but a single `SELECT` over allowed relations and columns. Reject joins, DDL/DML, comments, multiple statements, and unknown identifiers.
5. Execute with a read-only database role, statement timeout, row limit, and `EXPLAIN`/dry-run check before the real query.
6. Return data **and** the compiled SQL, semantic mappings, data freshness, and warnings. The UI can show these in the right-side data evidence tab.

If a question requires an unsupported calculation or ambiguity cannot be resolved from the semantic contract, the tool responds with a structured clarification request instead of guessing.

## Analytics evaluation suite

Create reviewed analytics cases in JSONL. Each case stores the question, expected `SemanticQuery`, expected mart, representative expected rows/aggregates, and a failure category.

Run the suite at three layers:

1. **Semantic parse:** valid questions map to expected approved concepts; unsupported phrases are rejected or clarified.
2. **Compiler safety:** compiled SQL parses as one allowed `SELECT`, binds parameters, honors the configured limit, and uses no raw relations.
3. **Result correctness:** outputs agree with a known fixture database for counts, totals, filters, and recommended-transfer constraints.

Examples include “Which critical recalls have Dallas stockout risk?”, “Can Memphis cover Dallas without dropping below safety stock?”, and “Which urgent campaigns have compatible customer vehicles but no matching SKU?”
