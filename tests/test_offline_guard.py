"""The test suite itself must stay offline and must never see a real credential."""

import socket

import anthropic
import pytest


def test_no_ambient_credentials():
    # What LLMClient builds when given no client. In tests there is no key in the
    # environment and the config folder is empty, so the SDK refuses to build a
    # client at all instead of finding a real credential somewhere.
    with pytest.raises(anthropic.CredentialsError):
        anthropic.AsyncAnthropic(max_retries=0)


def test_dns_is_blocked():
    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.getaddrinfo("api.anthropic.com", 443)
    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.create_connection(("api.anthropic.com", 443))


def test_no_anthropic_variable_left_except_empty_config_dir():
    import os

    left = {v for v in os.environ if v.upper().startswith("ANTHROPIC_")}
    assert left == {"ANTHROPIC_CONFIG_DIR"}
    assert os.listdir(os.environ["ANTHROPIC_CONFIG_DIR"]) == []
