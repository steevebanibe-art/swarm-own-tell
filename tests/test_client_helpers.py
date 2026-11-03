import anthropic
import httpx2
import pytest

from harness.client import cache_write_multiplier, cached_system, is_network_error
from harness.pricing import CACHE_WRITE_1H_MULTIPLIER, CACHE_WRITE_5M_MULTIPLIER


def test_cached_system_default_ttl():
    blocks = cached_system("You are a worker.")
    assert blocks == [{"type": "text", "text": "You are a worker.", "cache_control": {"type": "ephemeral"}}]
    req = {"model": "m", "max_tokens": 10, "system": blocks, "messages": []}
    assert cache_write_multiplier(req) == CACHE_WRITE_5M_MULTIPLIER


def test_cached_system_one_hour():
    blocks = cached_system("x", ttl="1h")
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    req = {"model": "m", "max_tokens": 10, "system": blocks, "messages": []}
    assert cache_write_multiplier(req) == CACHE_WRITE_1H_MULTIPLIER


def test_cached_system_rejects_bad_input():
    with pytest.raises(ValueError):
        cached_system("x", ttl="2h")
    with pytest.raises(ValueError):
        cached_system("")


def test_is_network_error():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    assert is_network_error(anthropic.APIConnectionError(request=req))
    assert is_network_error(anthropic.APITimeoutError(request=req))
    assert not is_network_error(ValueError("x"))
