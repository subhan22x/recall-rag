from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Local development configuration is intentionally optional. Production-style
# deployments can supply the same values through their environment instead.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str
    analytics_database_url: str
    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str
    embedding_model: str
    port: int
    cors_origins: list[str]
    mcp_http_token: str | None = None
    embedding_dimensions: int = 1536
    retrieval_limit: int = 8
    sql_max_rows: int = 100

    @property
    def model_enabled(self) -> bool:
        return bool(self.openai_api_key)


def get_settings() -> Settings:
    origins = os.getenv("RECALLOPS_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return Settings(
        database_url=os.getenv("DATABASE_URL", "postgresql://recallops:recallops@localhost:5433/recallops"),
        analytics_database_url=os.getenv("ANALYTICS_DATABASE_URL", "postgresql://recallops_ro:recallops_ro@localhost:5433/recallops"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
        embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        port=int(os.getenv("RECALLOPS_PORT", "8010")),
        cors_origins=[origin.strip() for origin in origins.split(",") if origin.strip()],
        mcp_http_token=os.getenv("RECALLOPS_MCP_HTTP_TOKEN") or None,
    )
