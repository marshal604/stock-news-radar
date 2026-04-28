from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def keywords_config() -> dict:
    with open(REPO_ROOT / "config" / "keywords.json", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.pop("_comment", None)
    return cfg


@pytest.fixture(scope="session")
def tickers_config() -> dict:
    with open(REPO_ROOT / "config" / "tickers.json", encoding="utf-8") as f:
        return json.load(f)


def _load_golden(folder: str) -> list[dict]:
    out = []
    for path in sorted((REPO_ROOT / "golden-set" / folder).glob("*.json")):
        with open(path, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


@pytest.fixture(scope="session")
def golden_positive() -> list[dict]:
    return _load_golden("positive")


@pytest.fixture(scope="session")
def golden_negative() -> list[dict]:
    return _load_golden("negative")
