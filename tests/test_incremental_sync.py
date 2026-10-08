"""Incremental-sync correctness, tested without Neo4j.

Covers the pieces of sync() that are pure logic:
- inclusive (>=) delta extraction closes the equal-stamp missed-row race
- the SyncState boundary-id guard keeps inclusive extraction idempotent
- incremental entity resolution (_resolve_delta): merges update names, titles
  and agencies instead of dropping them, and in-batch duplicates collapse
- the embedding-invalidation Cypher runs before the text is overwritten
"""

import copy
import sqlite3

from nexusvenue.etl import load as L
from nexusvenue.etl.extract import extract
from nexusvenue.etl.resolve import resolve_accounts, resolve_contacts
from nexusvenue.mockdata.generate import T0, generate, mutate_delta

W_OLD = "2026-07-01T00:00:00"
W_NEW = "2026-07-08T09:00:00"


# --------------------------------------------------------------------------
# extraction + boundary guard
# --------------------------------------------------------------------------

def _mini_crm(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE accounts (account_id TEXT, last_modified TEXT);
        CREATE TABLE agencies (agency_id TEXT, last_modified TEXT);
        CREATE TABLE contacts (contact_id TEXT, last_modified TEXT);
        CREATE TABLE rfps (rfp_id TEXT, last_modified TEXT);
        CREATE TABLE beo_history (beo_id TEXT, last_modified TEXT);
    """)
    con.executemany("INSERT INTO accounts VALUES (?, ?)",
                    [("A1", W_OLD), ("A2", W_OLD), ("A3", W_NEW)])
    con.executemany("INSERT INTO beo_history VALUES (?, ?)", [("B1", W_OLD), ("B2", W_NEW)])
    con.commit()
    con.close()
    return path


def test_extract_since_is_inclusive(tmp_path):
    db = _mini_crm(tmp_path / "crm.db")
    rows = extract(db_path=db, since=W_OLD)
    # >= must return the boundary rows too: a strict > silently loses any row
    # committed late with a stamp equal to the watermark
    assert {r["account_id"] for r in rows["accounts"]} == {"A1", "A2", "A3"}
    rows = extract(db_path=db, since=W_NEW)
    assert {r["account_id"] for r in rows["accounts"]} == {"A3"}
    assert {r["beo_id"] for r in rows["beo_history"]} == {"B2"}


def test_boundary_guard_round_trip(tmp_path):
    db = _mini_crm(tmp_path / "crm.db")
    full = extract(db_path=db)
    wm = L._max_modified(full)
    assert wm == W_NEW
    boundary = L._boundary_ids(full, wm)
    assert boundary == ["accounts:A3", "beo_history:B2"]

    # next sync: inclusive extraction re-reads the boundary rows...
    delta = extract(db_path=db, since=wm)
    assert delta["accounts"] and delta["beo_history"]
    # ...and the guard drops exactly them, so a re-run is a no-op
    assert not any(rows for rows in L._drop_boundary(delta, wm, boundary).values())

    # a row committed late with a stamp equal to the watermark IS caught
    con = sqlite3.connect(db)
    con.execute("INSERT INTO accounts VALUES ('A4-late', ?)", (wm,))
    con.commit()
    con.close()
    late = L._drop_boundary(extract(db_path=db, since=wm), wm, boundary)
    assert {r["account_id"] for r in late["accounts"]} == {"A4-late"}
    # and the refreshed boundary self-heals to cover it
    assert "accounts:A4-late" in L._boundary_ids(extract(db_path=db, since=wm), wm)


def test_graph_built_before_boundary_ids_reprocesses_once():
    raw = {"accounts": [{"account_id": "A1", "last_modified": W_NEW}],
           "agencies": [], "contacts": [], "rfps": [], "beo_history": []}
    # no stored boundary (older graph): nothing is dropped; upserts are idempotent
    assert L._drop_boundary(raw, W_NEW, None)["accounts"] == raw["accounts"]


def test_embedding_invalidation_precedes_text_overwrite():
    # the CASE must compare against the *previous* text, so it has to run before
    # the SET that overwrites it
    for query, text in [(L.BEO_UPSERT, "b.ops_notes = r.ops_notes"),
                        (L.RFP_UPSERT, "q.raw_text = r.raw_text")]:
        assert "embedding = CASE" in query
        assert query.index("embedding = CASE") < query.index(text)


# --------------------------------------------------------------------------
# incremental entity resolution on the real mock CRM + delta scenario
# --------------------------------------------------------------------------

def _graph_state(raw):
    """What the Neo4j graph holds after a full load, in sync()'s query shape."""
    accounts = [
        {"id": a["canonical_id"], "name": a["canonical_name"], "industry": a["industry"],
         "aliases": a["aliases"], "source_ids": a["source_ids"]}
        for a in resolve_accounts(raw["accounts"])
    ]
    planners = [
        {"id": c["canonical_id"], "email": c["email"], "name": c["full_name"],
         "title": c["title"], "agency_id": c["agency_id"], "source_ids": c["source_ids"]}
        for c in resolve_contacts(raw["contacts"])
    ]
    return accounts, planners


def test_demo_delta_resolves_the_way_the_readme_describes(tmp_path):
    db = tmp_path / "crm.db"
    generate(out_db=db, goldset_path=tmp_path / "goldset.json")
    full = extract(db_path=db)
    watermark = L._max_modified(full)
    assert watermark == T0
    boundary = L._boundary_ids(full, watermark)
    accounts, planners = _graph_state(full)
    acct_ids_before = {a["id"] for a in accounts}

    mutate_delta(db_path=db)
    raw = L._drop_boundary(extract(db_path=db, since=watermark), watermark, boundary)
    # without the guard an inclusive extraction would re-read the whole CRM;
    # with it only the business day's changes remain
    assert {t: len(rows) for t, rows in raw.items() if rows} == {
        "accounts": 2, "contacts": 2, "rfps": 2, "beo_history": 2}

    plan = L._resolve_delta(raw, copy.deepcopy(accounts), copy.deepcopy(planners))

    # a 4th name variant for an existing corporation merges, it does not fork
    [ashcombe] = [a for a in plan["merged_accounts"]
                  if any("Ashcombe" in al for al in a["aliases"])]
    assert "Ashcombe Incorporated" in ashcombe["aliases"]
    assert "CHI-ACC9001" in ashcombe["source_ids"]
    assert ashcombe["canonical_id"] in acct_ids_before
    assert len(ashcombe["canonical_name"]) < len("Ashcombe Incorporated")  # shortest variant keeps the name
    assert plan["account_rows_merged"] == 1

    # a brand-new account mints a new canonical node
    [bright] = plan["new_accounts"]
    assert bright["canonical_id"] == "ACCT-S001"
    assert bright["canonical_name"] == "Brightledger, Inc."

    # the duplicate person collapses on email; the new contact is minted
    dup_email = next(r["email"] for r in raw["contacts"] if r["contact_id"] == "CHI-CON9001")
    [merged] = [p for p in plan["merged_planners"] if "CHI-CON9001" in p["source_ids"]]
    existing = next(p for p in planners if (p["email"] or "").lower() == dup_email.lower())
    assert merged["canonical_id"] == existing["id"]
    assert len(merged["full_name"]) >= len(existing["name"]) or merged["full_name"] == existing["name"]
    [dana] = plan["new_planners"]
    assert dana["canonical_id"] == "PLNR-S001" and dana["full_name"] == "Dana Whitfield"

    # edges for the day's new BEOs/RFPs resolve to the right canonicals
    s2a, s2p = plan["src_to_account"], plan["src_to_planner"]
    assert s2a["MIA-ACC9002"] == bright["canonical_id"]
    assert s2p["MIA-CON9002"] == dana["canonical_id"]
    beo_new_account = next(r for r in raw["beo_history"] if r["beo_id"] == "ORL-BEO90001")["account_id"]
    assert s2a[beo_new_account] in acct_ids_before


def test_resolution_is_idempotent_on_replay(tmp_path):
    db = tmp_path / "crm.db"
    generate(out_db=db, goldset_path=tmp_path / "goldset.json")
    full = extract(db_path=db)
    watermark = L._max_modified(full)
    boundary = L._boundary_ids(full, watermark)
    accounts, planners = _graph_state(full)
    mutate_delta(db_path=db)
    raw = L._drop_boundary(extract(db_path=db, since=watermark), watermark, boundary)

    first = L._resolve_delta(raw, copy.deepcopy(accounts), copy.deepcopy(planners))

    # replay the same delta against the post-sync graph: nothing new is minted
    def as_graph(plan):
        accs = {a["id"]: a for a in copy.deepcopy(accounts)}
        for a in plan["accounts"]:
            accs[a["canonical_id"]] = {"id": a["canonical_id"], "name": a["canonical_name"],
                                       "industry": a["industry"], "aliases": a["aliases"],
                                       "source_ids": a["source_ids"]}
        pls = {p["id"]: p for p in copy.deepcopy(planners)}
        for p in plan["planners"]:
            pls[p["canonical_id"]] = {"id": p["canonical_id"], "email": p["email"],
                                      "name": p["full_name"], "title": p["title"],
                                      "agency_id": p["agency_id"], "source_ids": p["source_ids"]}
        return list(accs.values()), list(pls.values())

    g_accounts, g_planners = as_graph(first)
    second = L._resolve_delta(raw, copy.deepcopy(g_accounts), copy.deepcopy(g_planners))
    assert not second["new_accounts"] and not second["new_planners"]
    assert {a["canonical_id"]: a["aliases"] for a in second["accounts"]} == \
           {a["canonical_id"]: a["aliases"] for a in first["accounts"]}
    assert {p["canonical_id"]: p["source_ids"] for p in second["planners"]} == \
           {p["canonical_id"]: p["source_ids"] for p in first["planners"]}


# --------------------------------------------------------------------------
# merge rules: what a merge may and may not change
# --------------------------------------------------------------------------

def _acct(aid, name, industry="Retail"):
    return {"account_id": aid, "account_name": name, "industry": industry}


def _contact(cid, name, email, title="Events Director", agency=None):
    return {"contact_id": cid, "full_name": name, "email": email, "title": title, "agency_id": agency}


def _empty(**kw):
    raw = {"accounts": [], "agencies": [], "contacts": [], "rfps": [], "beo_history": []}
    raw.update(kw)
    return raw


EXISTING_ACCT = [{"id": "ACCT-0001", "name": "Wexford Markets", "industry": "Retail",
                  "aliases": ["Wexford Markets"], "source_ids": ["ORL-ACC0001"]}]
EXISTING_PLNR = [{"id": "PLNR-0001", "email": "kim@wexford.com", "name": "Kim Osei",
                  "title": "Events Director", "agency_id": None, "source_ids": ["ORL-CON0001"]}]


def test_shorter_variant_renames_canonical_account_longer_does_not():
    # all three spellings normalize to "wexford markets" (legal suffixes dropped)
    existing = [{"id": "ACCT-0001", "name": "Wexford Markets Inc", "industry": "Retail",
                 "aliases": ["Wexford Markets Inc"], "source_ids": ["ORL-ACC0001"]}]
    longer = L._resolve_delta(_empty(accounts=[_acct("CHI-ACC9", "Wexford Markets Group")]),
                              copy.deepcopy(existing), [])
    assert longer["merged_accounts"][0]["canonical_name"] == "Wexford Markets Inc"  # unchanged

    plan = L._resolve_delta(
        _empty(accounts=[_acct("CHI-ACC9", "Wexford Markets Group"), _acct("MIA-ACC9", "Wexford Markets")]),
        copy.deepcopy(existing), [])
    [a] = plan["merged_accounts"]
    # strictly shorter than the current name -> it becomes the display name,
    # exactly as a full rebuild (resolve_accounts: shortest variant) would pick
    assert a["canonical_name"] == "Wexford Markets"
    assert set(a["aliases"]) == {"Wexford Markets Inc", "Wexford Markets Group", "Wexford Markets"}
    assert plan["account_rows_merged"] == 2


def test_industry_is_first_wins_unless_a_known_row_is_corrected():
    # a NEW source row for an existing company must not flip its industry...
    plan = L._resolve_delta(_empty(accounts=[_acct("CHI-ACC9", "Wexford Markets Inc", "Grocery")]),
                            copy.deepcopy(EXISTING_ACCT), [])
    assert plan["merged_accounts"][0]["industry"] == "Retail"
    # ...but a row restating a KNOWN source id is a CRM correction and lands
    plan = L._resolve_delta(_empty(accounts=[_acct("ORL-ACC0001", "Wexford Markets", "Grocery")]),
                            copy.deepcopy(EXISTING_ACCT), [])
    assert plan["merged_accounts"][0]["industry"] == "Grocery"


def test_planner_title_update_lands_only_for_a_restated_source_row():
    # same source row, new title: a correction -> must land (was silently dropped)
    plan = L._resolve_delta(
        _empty(contacts=[_contact("ORL-CON0001", "Kim Osei", "KIM@wexford.com", title="VP Events")]),
        [], copy.deepcopy(EXISTING_PLNR))
    [p] = plan["merged_planners"]
    assert p["title"] == "VP Events" and p["canonical_id"] == "PLNR-0001"
    # a different source row for the same person: first title wins, spelling grows
    plan = L._resolve_delta(
        _empty(contacts=[_contact("CHI-CON0007", "Kimberly Osei", "kim@wexford.com", title="Assistant")]),
        [], copy.deepcopy(EXISTING_PLNR))
    [p] = plan["merged_planners"]
    assert p["title"] == "Events Director"
    assert p["full_name"] == "Kimberly Osei"  # longest spelling wins, as in resolve_contacts
    assert set(p["source_ids"]) == {"ORL-CON0001", "CHI-CON0007"}


def test_agency_is_filled_in_but_never_reassigned():
    plan = L._resolve_delta(
        _empty(contacts=[_contact("CHI-CON0007", "Kim Osei", "kim@wexford.com", agency="AG003")]),
        [], copy.deepcopy(EXISTING_PLNR))
    assert plan["merged_planners"][0]["agency_id"] == "AG003"   # was missing -> filled
    placed = copy.deepcopy(EXISTING_PLNR)
    placed[0]["agency_id"] = "AG001"
    plan = L._resolve_delta(
        _empty(contacts=[_contact("CHI-CON0007", "Kim Osei", "kim@wexford.com", agency="AG003")]),
        [], placed)
    assert plan["merged_planners"][0]["agency_id"] == "AG001"   # first non-null wins


# --------------------------------------------------------------------------
# in-batch duplicates
# --------------------------------------------------------------------------

def test_two_variants_of_one_new_company_collapse_onto_one_node():
    plan = L._resolve_delta(
        _empty(accounts=[_acct("MIA-ACC9", "Zephyrine Labs, Inc."), _acct("CHI-ACC9", "Zephyrine Labs")]),
        copy.deepcopy(EXISTING_ACCT), [])
    [z] = plan["new_accounts"]
    assert z["canonical_id"] == "ACCT-S001"
    assert set(z["aliases"]) == {"Zephyrine Labs, Inc.", "Zephyrine Labs"}
    assert z["canonical_name"] == "Zephyrine Labs"
    assert plan["src_to_account"]["MIA-ACC9"] == plan["src_to_account"]["CHI-ACC9"] == "ACCT-S001"


def test_second_row_for_a_planner_minted_in_the_same_batch_keeps_its_source_id():
    # regression: the second row used to merge into a detached copy, so its
    # source id never reached the canonical node or the BEO -> planner map
    plan = L._resolve_delta(
        _empty(contacts=[_contact("MIA-CON9", "Dana Whitfield", "dana@brightledger.com"),
                         _contact("CHI-CON9", "D. Whitfield", "DANA@brightledger.com")]),
        [], copy.deepcopy(EXISTING_PLNR))
    [dana] = plan["new_planners"]
    assert set(dana["source_ids"]) == {"MIA-CON9", "CHI-CON9"}
    assert not plan["merged_planners"]                      # nothing existing was touched
    assert plan["src_to_planner"]["MIA-CON9"] == plan["src_to_planner"]["CHI-CON9"] == dana["canonical_id"]


def test_minted_ids_skip_ids_already_taken():
    existing = copy.deepcopy(EXISTING_ACCT) + [
        {"id": "ACCT-S001", "name": "Old Delta Co", "industry": None,
         "aliases": ["Old Delta Co"], "source_ids": ["X-1"]}]
    plan = L._resolve_delta(_empty(accounts=[_acct("MIA-ACC9", "Brand New Holdings Ltd")]), existing, [])
    assert [a["canonical_id"] for a in plan["new_accounts"]] == ["ACCT-S002"]
