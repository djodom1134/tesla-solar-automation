import os

os.environ["DEMO"] = "1"

import pytest


@pytest.fixture(autouse=True)
def _no_gas_price_fetch(monkeypatch, request):
    """The collector loop fetches the EIA gas price daily; run() tests must
    never reach the network for it. test_gas.py exercises refresh() itself
    against a mock transport, so it is left alone."""
    if request.module.__name__.endswith("test_gas"):
        return
    import gas

    async def offline(db):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(gas, "refresh", offline)
