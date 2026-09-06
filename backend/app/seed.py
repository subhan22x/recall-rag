from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

from .config import Settings
from .db import connection

WAREHOUSES = ["DFW-01", "HOU-01", "AUS-01", "SAT-01", "MEM-02", "PHX-01"]
VEHICLES = [
    ("FORD", "F-150", 2020),
    ("HONDA", "CIVIC", 2021),
    ("TOYOTA", "CAMRY", 2022),
    ("CHEVROLET", "SILVERADO 1500", 2021),
    ("TESLA", "MODEL 3", 2022),
]
CATEGORIES = ["brake_hose", "hydraulic_line", "brake_pedal", "airbag_module", "fuel_line", "steering_linkage"]
RECALLS = [
    {
        "campaign_id": "24V-118", "manufacturer": "Ford Motor Company", "component": "SERVICE BRAKES, HYDRAULIC",
        "summary": "Certain vehicles may experience a brake hose rupture that can cause a loss of brake fluid.",
        "consequence": "A loss of brake fluid can reduce braking performance and increase crash risk.",
        "remedy": "Dealers will replace the brake hose free of charge.", "park_outside": False, "do_not_drive": True,
        "report_date": date(2024, 3, 18), "vehicle": VEHICLES[0],
    },
    {
        "campaign_id": "23V-441", "manufacturer": "Toyota Motor Engineering", "component": "SERVICE BRAKES, HYDRAULIC",
        "summary": "A hydraulic line may crack and leak brake fluid under certain conditions.",
        "consequence": "Reduced brake performance may increase the risk of a crash.",
        "remedy": "Dealers will inspect and replace the affected line at no cost.", "park_outside": True, "do_not_drive": False,
        "report_date": date(2023, 11, 2), "vehicle": VEHICLES[2],
    },
    {
        "campaign_id": "22V-307", "manufacturer": "Honda (American Honda Motor Co.)", "component": "SERVICE BRAKES, PEDAL",
        "summary": "The brake pedal assembly may not have been manufactured to specification.",
        "consequence": "The condition can increase stopping distance.",
        "remedy": "Honda will replace the pedal assembly free of charge.", "park_outside": False, "do_not_drive": False,
        "report_date": date(2022, 5, 21), "vehicle": VEHICLES[1],
    },
    {
        "campaign_id": "24V-601", "manufacturer": "Tesla, Inc.", "component": "AIR BAGS",
        "summary": "An airbag warning indicator may not alert the driver to a potential restraint-system fault.",
        "consequence": "A missing warning could delay repair of a restraint-system condition.",
        "remedy": "Tesla will provide an over-the-air software update.", "park_outside": False, "do_not_drive": False,
        "report_date": date(2024, 8, 9), "vehicle": VEHICLES[4],
    },
]


def seed_demo_data(settings: Settings) -> None:
    """Create repeatable, explicitly synthetic distributor data for local development."""
    now = datetime.now(UTC)
    randomizer = random.Random(23)
    with connection(settings) as conn:
        if conn.execute("SELECT count(*) AS count FROM raw_product_catalog").fetchone()["count"]:
            return

        for recall in RECALLS:
            make, model, year = recall["vehicle"]
            source_key = f"nhtsa:{recall['campaign_id']}:{make}:{model}:{year}"
            conn.execute(
                """INSERT INTO raw_nhtsa_recalls
                (source_key,campaign_id,manufacturer,component,summary,consequence,remedy,park_outside,do_not_drive,report_date,source_url,source_refreshed_at)
                VALUES (%(source_key)s,%(campaign_id)s,%(manufacturer)s,%(component)s,%(summary)s,%(consequence)s,%(remedy)s,%(park_outside)s,%(do_not_drive)s,%(report_date)s,%(source_url)s,%(refreshed)s)""",
                {**recall, "source_key": source_key, "source_url": "https://www.nhtsa.gov/recalls", "refreshed": now},
            )
            conn.execute(
                """INSERT INTO raw_nhtsa_recall_vehicles (source_key,campaign_id,make,model,model_year,source_refreshed_at)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (source_key, recall["campaign_id"], make, model, year, now),
            )

        products = []
        for index in range(300):
            category = CATEGORIES[index % len(CATEGORIES)]
            sku = f"{category[:3].upper()}-{index + 1:04d}"
            products.append((sku, f"Synthetic {category.replace('_', ' ')} {index + 1}", category, round(15 + (index % 30) * 3.1, 2), now))
        conn.cursor().executemany(
            "INSERT INTO raw_product_catalog (sku,product_name,product_category,unit_cost,source_refreshed_at) VALUES (%s,%s,%s,%s,%s)",
            products,
        )

        fitment = []
        for index, product in enumerate(products):
            sku, _, category, _, _ = product
            if category in {"brake_hose", "hydraulic_line", "brake_pedal"}:
                vehicle = VEHICLES[index % 3]
            else:
                vehicle = VEHICLES[index % len(VEHICLES)]
            fitment.append((sku, *vehicle))
        conn.cursor().executemany("INSERT INTO raw_vehicle_fitment (sku,make,model,model_year) VALUES (%s,%s,%s,%s)", fitment)

        fleet_rows = []
        for index in range(500):
            make, model, year = VEHICLES[index % len(VEHICLES)]
            fleet_rows.append((f"fleet-{index + 1:04d}", WAREHOUSES[index % len(WAREHOUSES)], make, model, year, now))
        conn.cursor().executemany(
            "INSERT INTO raw_customer_fleet (vehicle_id,warehouse_id,make,model,model_year,source_refreshed_at) VALUES (%s,%s,%s,%s,%s,%s)",
            fleet_rows,
        )

        sales_rows = []
        start = date.today() - timedelta(days=180)
        for day_offset in range(180):
            order_date = start + timedelta(days=day_offset)
            for index in range(0, 300, 6):
                sku = products[index][0]
                sales_rows.append((f"sales-{day_offset:03d}-{index:03d}", order_date, WAREHOUSES[(index + day_offset) % len(WAREHOUSES)], sku, 1 + ((index + day_offset) % 7), now))
        conn.cursor().executemany(
            "INSERT INTO raw_sales_orders (order_line_id,order_date,warehouse_id,sku,quantity,source_refreshed_at) VALUES (%s,%s,%s,%s,%s,%s)",
            sales_rows,
        )

        snapshot_date = date.today()
        inventory_rows, safety_rows, open_order_rows = [], [], []
        for warehouse in WAREHOUSES:
            for index, product in enumerate(products):
                sku = product[0]
                on_hand = 20 + ((index * 7 + len(warehouse)) % 120)
                if warehouse == "DFW-01" and product[2] in {"brake_hose", "hydraulic_line"}:
                    on_hand = 14 + (index % 10)
                if warehouse == "MEM-02" and product[2] in {"brake_hose", "hydraulic_line"}:
                    on_hand = 175 + (index % 25)
                reserved = index % 9
                damaged = index % 3
                inventory_rows.append((snapshot_date, warehouse, sku, on_hand, reserved, damaged, now))
                safety_rows.append((warehouse, sku, 15 + index % 12))
                if index % 5 == 0:
                    open_order_rows.append((f"open-{warehouse}-{sku}", warehouse, sku, 5 + index % 20, snapshot_date + timedelta(days=7), now))
        conn.cursor().executemany(
            "INSERT INTO raw_inventory_snapshots (snapshot_date,warehouse_id,sku,on_hand,reserved,damaged,source_refreshed_at) VALUES (%s,%s,%s,%s,%s,%s,%s)", inventory_rows
        )
        conn.cursor().executemany("INSERT INTO raw_safety_stock_rules (warehouse_id,sku,safety_stock_units) VALUES (%s,%s,%s)", safety_rows)
        conn.cursor().executemany(
            "INSERT INTO raw_open_sales_orders (order_line_id,warehouse_id,sku,quantity,due_date,source_refreshed_at) VALUES (%s,%s,%s,%s,%s,%s)", open_order_rows
        )

        purchase_rows = []
        for index in range(100):
            sku = products[index * 3][0]
            purchase_rows.append((f"po-{index + 1:03d}", WAREHOUSES[index % len(WAREHOUSES)], sku, 25 + index % 40, snapshot_date + timedelta(days=1 + index % 14), "open", now))
        conn.cursor().executemany(
            "INSERT INTO raw_purchase_orders (purchase_order_id,warehouse_id,sku,quantity,expected_date,status,source_refreshed_at) VALUES (%s,%s,%s,%s,%s,%s,%s)", purchase_rows
        )

        route_rows = []
        for source in WAREHOUSES:
            for destination in WAREHOUSES:
                if source != destination:
                    route_rows.append((source, destination, 1 + ((len(source) + len(destination)) % 3), True))
        conn.cursor().executemany("INSERT INTO raw_warehouse_routes (source_warehouse_id,destination_warehouse_id,lead_time_days,active) VALUES (%s,%s,%s,%s)", route_rows)

        crosswalk_rows = [
            ("SERVICE BRAKES, HYDRAULIC", "brake_hose", "2026-01", True),
            ("SERVICE BRAKES, HYDRAULIC", "hydraulic_line", "2026-01", True),
            ("SERVICE BRAKES, PEDAL", "brake_pedal", "2026-01", True),
            ("AIR BAGS", "airbag_module", "2026-01", True),
        ]
        conn.cursor().executemany("INSERT INTO raw_component_product_crosswalk (component_pattern,product_category,version,active) VALUES (%s,%s,%s,%s)", crosswalk_rows)
        conn.commit()
