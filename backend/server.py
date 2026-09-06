"""Compatibility launcher for the Recall RAG FastAPI implementation.

Run from the repository root with either `python backend/server.py` when the
required packages are installed, or `.venv/bin/python backend/server.py` for
the local development environment documented in the README.
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, reload=False)
