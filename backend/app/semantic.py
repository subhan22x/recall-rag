from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_LAYER_PATH = ROOT / "semantic_layer.yml"


def load_semantic_layer() -> dict[str, Any]:
    with SEMANTIC_LAYER_PATH.open() as file:
        layer = yaml.safe_load(file)
    if not isinstance(layer, dict) or not isinstance(layer.get("models"), dict):
        raise ValueError("semantic_layer.yml must define a models mapping")
    return layer


def public_semantic_layer() -> dict[str, Any]:
    """The full YAML is safe to show; it contains only data definitions, never credentials."""
    return load_semantic_layer()
