import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "turbo: run with the TURBO settings instead of the runner-era values")


@pytest.fixture(autouse=True)
def _settings(request, monkeypatch):
    """Rule tests check the runner-era numbers; tests marked @pytest.mark.turbo check the turbo numbers."""
    from desk import config as C
    use = C.TURBO_SETTINGS if request.node.get_closest_marker("turbo") else C.BASE_SETTINGS
    for k, v in use.items():
        monkeypatch.setattr(C, k, v)
