"""C1: DiscordPostError must distinguish retryable (transient) from permanent.

Pipeline uses this to decide whether to mark_seen — transient failure leaves
item un-marked so next run retries; permanent failure marks seen to avoid loop."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.discord as discord_mod
from src.discord import DiscordPostError, post_discord


def _mock_resp(status_code: int, text: str = ""):
    m = MagicMock()
    m.status_code = status_code
    m.text = text
    return m


def _patch_client(monkeypatch, response):
    """Make httpx.Client(...) return a context-manager that .post() → response."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.post.return_value = response

    factory = MagicMock(return_value=mock_client)
    monkeypatch.setattr(discord_mod.httpx, "Client", factory)


def test_2xx_returns_true(monkeypatch):
    _patch_client(monkeypatch, _mock_resp(204))
    assert post_discord("hi", webhook_url="https://example.com/hook") is True


def test_4xx_raises_permanent(monkeypatch):
    _patch_client(monkeypatch, _mock_resp(400, "bad request"))
    with pytest.raises(DiscordPostError) as exc_info:
        post_discord("hi", webhook_url="https://example.com/hook")
    assert exc_info.value.retryable is False


def test_5xx_after_retries_raises_retryable(monkeypatch):
    """5xx exhausts retries and surfaces as retryable=True so next run retries."""
    monkeypatch.setattr("src.discord._RETRY_BACKOFF_SEC", (0, 0, 0))
    _patch_client(monkeypatch, _mock_resp(503, "service down"))
    with pytest.raises(DiscordPostError) as exc_info:
        post_discord("hi", webhook_url="https://example.com/hook")
    assert exc_info.value.retryable is True


def test_429_after_retries_raises_retryable(monkeypatch):
    monkeypatch.setattr("src.discord._RETRY_BACKOFF_SEC", (0, 0, 0))
    _patch_client(monkeypatch, _mock_resp(429, "rate limited"))
    with pytest.raises(DiscordPostError) as exc_info:
        post_discord("hi", webhook_url="https://example.com/hook")
    assert exc_info.value.retryable is True


def test_no_webhook_returns_false():
    """No webhook configured = no-op, returns False (used by dry-run path)."""
    assert post_discord("hi", webhook_url="") is False
    assert post_discord("hi", webhook_url=None) is False
