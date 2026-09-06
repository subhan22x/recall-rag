from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import Settings


@contextmanager
def connection(settings: Settings, database_url: str | None = None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(database_url or settings.database_url, row_factory=dict_row) as conn:
        # Initial bootstrap happens before CREATE EXTENSION vector. Subsequent
        # connections register the pgvector adapter for typed vector queries.
        try:
            register_vector(conn)
        except psycopg.ProgrammingError:
            pass
        yield conn


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS analytics;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'recallops_ro') THEN
    CREATE ROLE recallops_ro LOGIN PASSWORD 'recallops_ro' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO recallops_ro', current_database());
END $$;
GRANT USAGE ON SCHEMA analytics TO recallops_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO recallops_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO recallops_ro;

CREATE TABLE IF NOT EXISTS raw_nhtsa_recalls (
  source_key text PRIMARY KEY,
  campaign_id text NOT NULL,
  manufacturer text NOT NULL,
  component text NOT NULL,
  summary text NOT NULL,
  consequence text NOT NULL,
  remedy text NOT NULL,
  park_outside boolean NOT NULL DEFAULT false,
  do_not_drive boolean NOT NULL DEFAULT false,
  report_date date,
  source_url text NOT NULL,
  source_refreshed_at timestamptz NOT NULL,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS raw_nhtsa_recall_vehicles (
  source_key text PRIMARY KEY,
  campaign_id text NOT NULL,
  make text NOT NULL,
  model text NOT NULL,
  model_year integer NOT NULL,
  source_refreshed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_product_catalog (
  sku text PRIMARY KEY,
  product_name text NOT NULL,
  product_category text NOT NULL,
  unit_cost numeric(12,2) NOT NULL,
  active boolean NOT NULL DEFAULT true,
  source_refreshed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_vehicle_fitment (
  sku text NOT NULL REFERENCES raw_product_catalog(sku),
  make text NOT NULL,
  model text NOT NULL,
  model_year integer NOT NULL,
  PRIMARY KEY (sku, make, model, model_year)
);

CREATE TABLE IF NOT EXISTS raw_customer_fleet (
  vehicle_id text PRIMARY KEY,
  warehouse_id text NOT NULL,
  make text NOT NULL,
  model text NOT NULL,
  model_year integer NOT NULL,
  active boolean NOT NULL DEFAULT true,
  source_refreshed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_sales_orders (
  order_line_id text PRIMARY KEY,
  order_date date NOT NULL,
  warehouse_id text NOT NULL,
  sku text NOT NULL REFERENCES raw_product_catalog(sku),
  quantity integer NOT NULL CHECK (quantity > 0),
  source_refreshed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_inventory_snapshots (
  snapshot_date date NOT NULL,
  warehouse_id text NOT NULL,
  sku text NOT NULL REFERENCES raw_product_catalog(sku),
  on_hand integer NOT NULL CHECK (on_hand >= 0),
  reserved integer NOT NULL CHECK (reserved >= 0),
  damaged integer NOT NULL CHECK (damaged >= 0),
  source_refreshed_at timestamptz NOT NULL,
  PRIMARY KEY (snapshot_date, warehouse_id, sku)
);

CREATE TABLE IF NOT EXISTS raw_open_sales_orders (
  order_line_id text PRIMARY KEY,
  warehouse_id text NOT NULL,
  sku text NOT NULL REFERENCES raw_product_catalog(sku),
  quantity integer NOT NULL CHECK (quantity > 0),
  due_date date NOT NULL,
  source_refreshed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_purchase_orders (
  purchase_order_id text PRIMARY KEY,
  warehouse_id text NOT NULL,
  sku text NOT NULL REFERENCES raw_product_catalog(sku),
  quantity integer NOT NULL CHECK (quantity > 0),
  expected_date date NOT NULL,
  status text NOT NULL,
  source_refreshed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_warehouse_routes (
  source_warehouse_id text NOT NULL,
  destination_warehouse_id text NOT NULL,
  lead_time_days integer NOT NULL CHECK (lead_time_days >= 0),
  active boolean NOT NULL DEFAULT true,
  PRIMARY KEY (source_warehouse_id, destination_warehouse_id)
);

CREATE TABLE IF NOT EXISTS raw_safety_stock_rules (
  warehouse_id text NOT NULL,
  sku text NOT NULL REFERENCES raw_product_catalog(sku),
  safety_stock_units integer NOT NULL CHECK (safety_stock_units >= 0),
  PRIMARY KEY (warehouse_id, sku)
);

CREATE TABLE IF NOT EXISTS raw_component_product_crosswalk (
  component_pattern text NOT NULL,
  product_category text NOT NULL,
  version text NOT NULL,
  active boolean NOT NULL DEFAULT true,
  PRIMARY KEY (component_pattern, product_category, version)
);

CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY,
  source_key text UNIQUE NOT NULL,
  source_type text NOT NULL,
  canonical_url text NOT NULL,
  title text NOT NULL,
  mime_type text NOT NULL,
  active_version_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_versions (
  id uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES documents(id),
  checksum text NOT NULL,
  source_updated_at timestamptz,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  etag text,
  last_modified text,
  storage_path text,
  parser_version text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  UNIQUE (document_id, checksum)
);

CREATE TABLE IF NOT EXISTS document_pages (
  id uuid PRIMARY KEY,
  document_version_id uuid NOT NULL REFERENCES document_versions(id),
  page_number integer NOT NULL,
  page_text text NOT NULL,
  width numeric,
  height numeric,
  UNIQUE (document_version_id, page_number)
);

CREATE TABLE IF NOT EXISTS document_chunks (
  id uuid PRIMARY KEY,
  document_version_id uuid NOT NULL REFERENCES document_versions(id),
  page_id uuid REFERENCES document_pages(id),
  chunk_index integer NOT NULL,
  text text NOT NULL,
  token_count integer NOT NULL,
  char_start integer NOT NULL,
  char_end integer NOT NULL,
  bbox_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  embedding vector(1536) NOT NULL,
  embedding_model text NOT NULL,
  content_hash text NOT NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  UNIQUE (document_version_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS document_chunks_search_idx ON document_chunks USING gin(search_tsv);
CREATE INDEX IF NOT EXISTS document_chunks_embedding_hnsw ON document_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS evaluation_runs (
  id uuid PRIMARY KEY,
  suite text NOT NULL,
  configuration jsonb NOT NULL,
  results jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mcp_tool_calls (
  id uuid PRIMARY KEY,
  request_id text NOT NULL,
  client_name text NOT NULL,
  tool_name text NOT NULL,
  status text NOT NULL CHECK (status IN ('success','no_result','clarification_required','refused','error')),
  result_summary text NOT NULL,
  result_count integer NOT NULL DEFAULT 0 CHECK (result_count >= 0),
  duration_ms integer NOT NULL CHECK (duration_ms >= 0),
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mcp_tool_calls_created_idx ON mcp_tool_calls (created_at DESC);
"""


def init_database(settings: Settings) -> None:
    with connection(settings) as conn:
        schema_sql = SCHEMA_SQL
        if os.getenv("RECALLOPS_SKIP_READONLY_ROLE") == "1":
            role_setup_start = schema_sql.index("DO $$\nBEGIN")
            role_setup_end = schema_sql.index("CREATE TABLE IF NOT EXISTS raw_nhtsa_recalls")
            schema_sql = schema_sql[:role_setup_start] + schema_sql[role_setup_end:]
        conn.execute(schema_sql)
        conn.commit()
