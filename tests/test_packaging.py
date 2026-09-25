"""Guards on what actually ships in the wheel.

CI and local runs use `pip install -e .`, where every file in the source tree is
importable whether or not package-data matches it. The image installs a real
wheel, so a template the patterns miss is present in tests and absent in
production -- which is exactly how email_digest.txt shipped broken.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _package_data_patterns() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return config["tool"]["setuptools"]["package-data"]["aem"]


def test_every_template_is_covered_by_package_data():
    patterns = _package_data_patterns()
    templates = sorted(p for p in (ROOT / "src/aem/web/templates").iterdir() if p.is_file())
    assert templates, "no templates found -- wrong path?"
    missing = [
        f"web/templates/{p.name}"
        for p in templates
        if not any(fnmatch.fnmatch(f"web/templates/{p.name}", pat) for pat in patterns)
    ]
    assert not missing, f"not shipped in the wheel: {missing} (patterns: {patterns})"
