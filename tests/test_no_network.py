"""The root conftest.py blocks the network for every test."""

import socket

import pytest


def test_opening_a_connection_fails():
    with pytest.raises(RuntimeError, match="network access in a test"):
        socket.create_connection(("example.com", 443), timeout=1)
