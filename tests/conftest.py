from __future__ import annotations

import pytest

from asap.harness import Env


@pytest.fixture
def env(tmp_path):
    e = Env.create(tmp_path / "runs")
    yield e
    e.close()


needs_policy = pytest.mark.skipif(
    __import__("asap.control.policy", fromlist=["PolicyEngine"]).PolicyEngine().name.startswith("none"),
    reason="no Rego evaluator installed (run `make setup`)")
