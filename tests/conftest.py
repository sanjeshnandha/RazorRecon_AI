import json
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANUAL = ROOT / "tests" / "golden" / "manual"


@pytest.fixture(scope="session")
def db():
    from engine.db import connect
    conn = connect()
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def fixtures():
    return {p.stem: json.loads(p.read_text()) for p in sorted(MANUAL.glob("*.json"))}


@pytest.fixture(scope="session")
def demo_run(db):
    """One seeded 100-settlement dataset + one engine run, shared by the golden tests."""
    from engine import runner
    from engine.policy import load_policy
    from generator.generate import build, persist
    ds = build(42, 100, load_policy(), "golden-tests")
    # the dataset_id is derived from the seed, so a re-run of the suite would
    # collide. Datasets cascade, so this clears the whole prior tree.
    with db.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
    db.commit()
    persist(ds, db)
    m = runner.run(db, ds.dataset_id)
    return {"dataset_id": ds.dataset_id, "run_id": m["run_id"], "metrics": m, "ds": ds}
