"""Time-sliced retrieval plumbing, tested with a fake Neo4j driver.

The Cypher itself needs a live database, but the contract that matters for
honest backtests is checkable here: as_of reaches every query, the vector
index is over-fetched when slicing, the time-sliced agency query is used (the
REPRESENTS edge is inferred from all bookings and would leak the future), and
the default path is untouched.
"""

import pytest

from nexusvenue.rag import retrieve as R
from nexusvenue.rag.embed import HashEmbedder


class _Single:
    def __init__(self, row):
        self._row = row

    def data(self):
        return self._row


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows

    def single(self):
        return _Single(self._rows[0])


class _Session:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        self.calls.append((query, params))
        if query is R.VECTOR_QUERY:
            return _Result([{
                "score": 0.9, "beo_id": "ORL-BEO00001", "event_type": "Annual Gala",
                "event_date": "2024-05-01", "attendees": 100, "fb_spend": 1.0, "av_spend": 1.0,
                "total_revenue": 3.0, "ops_notes": "n", "account": "Ashcombe", "account_id": "ACCT-0001",
                "property": "NexusVenue Grand Orlando", "planner": "Kim Osei",
                "agency": "Meridian Event Partners"}])
        if query is R.ACCOUNT_PORTFOLIO:
            return _Result([{"account": "Ashcombe", "events": 1}])
        return _Result([{"agency": "Meridian Event Partners", "total_events": 1}])


class _Driver:
    def __init__(self):
        self.calls = []

    def session(self):
        return _Session(self.calls)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def hash_embedder(monkeypatch):
    monkeypatch.setattr(R, "get_embedder", lambda: HashEmbedder(8))


def test_default_path_is_unsliced():
    d = _Driver()
    R.retrieve("gala", k=6, driver=d)
    by_query = {q: p for q, p in d.calls}
    assert by_query[R.VECTOR_QUERY]["fetch_k"] == 6 and by_query[R.VECTOR_QUERY]["k"] == 6
    assert all(p["as_of"] is None for _, p in d.calls)
    assert R.AGENCY_BOOK in by_query and R.AGENCY_BOOK_AS_OF not in by_query


def test_as_of_reaches_every_query_and_overfetches():
    d = _Driver()
    out = R.retrieve("gala", k=6, driver=d, as_of="2025-03-01")
    by_query = {q: p for q, p in d.calls}
    assert all(p["as_of"] == "2025-03-01" for _, p in d.calls)
    assert by_query[R.VECTOR_QUERY]["k"] == 6
    assert by_query[R.VECTOR_QUERY]["fetch_k"] >= R.SLICE_OVERFETCH_MIN > 6
    # REPRESENTS leaks the future, so the sliced agency query must be used
    assert R.AGENCY_BOOK_AS_OF in by_query and R.AGENCY_BOOK not in by_query
    assert out["similar_past_events"][0]["beo_id"] == "ORL-BEO00001"
    assert "account_id" not in out["similar_past_events"][0]


def test_every_traversal_query_filters_on_the_cutoff():
    assert "$as_of IS NULL OR b.event_date < $as_of" in R.VECTOR_QUERY
    assert "$as_of IS NULL OR b.event_date < $as_of" in R.ACCOUNT_PORTFOLIO
    # the sliced agency query rebuilds "represents" from pre-cutoff bookings
    assert "rb.event_date < $as_of" in R.AGENCY_BOOK_AS_OF
    assert "b.event_date < $as_of" in R.AGENCY_BOOK_AS_OF
    assert "REPRESENTS" not in R.AGENCY_BOOK_AS_OF
