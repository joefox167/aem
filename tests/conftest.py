from pathlib import Path

import pytest

from aem.config import AppConfig
from aem.db import init_db, make_engine, make_session_factory

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def session_factory(tmp_path):
    engine = make_engine(str(tmp_path / "test.db"))
    init_db(engine)
    return make_session_factory(engine)


@pytest.fixture
def cfg():
    return AppConfig()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()
