"""Smoke test so CI is green from the first commit. Real tests arrive with each rule."""

import reconciler


def test_package_imports() -> None:
    assert reconciler.__version__ == "0.1.0"
