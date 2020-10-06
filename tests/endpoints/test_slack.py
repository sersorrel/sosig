import asyncio

import pytest

from sosig.endpoints.slack import DecayingDict, ExpiredKeyError


@pytest.mark.asyncio
async def test_decayingdict(event_loop):
    d = DecayingDict(1, x="y")
    d["a"] = "b"
    assert d["a"] == "b"
    assert d["x"] == "y"
    with pytest.raises(KeyError):
        d["b"]
    await asyncio.sleep(2)
    with pytest.raises(ExpiredKeyError):
        d["a"]
    with pytest.raises(ExpiredKeyError):
        d["x"]


@pytest.mark.asyncio
async def test_decayingdict2(event_loop):
    d = DecayingDict(2, x="y")
    d["a"] = "b"
    assert d["a"] == "b"
    assert d["x"] == "y"
    with pytest.raises(KeyError):
        d["b"]
    await asyncio.sleep(1.5)
    assert d["a"] == "b"
    await asyncio.sleep(1)
    with pytest.raises(ExpiredKeyError):
        d["a"]
    with pytest.raises(ExpiredKeyError):
        d["x"]
