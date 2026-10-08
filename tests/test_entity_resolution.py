"""Account entity resolution: names first, contact evidence second, precision always.

The resolver recovers companies that name matching alone can't connect
("LMK" for "Luminark") by looking at the contacts filed under each account.
These tests pin the part that matters most: it must fail safe. Conflicting or
sparse evidence may cost recall, but never merges two companies.
"""

import pytest

from nexusvenue.etl import load as L
from nexusvenue.etl.extract import extract
from nexusvenue.etl.resolve import (
    account_domains,
    choose_display_name,
    email_domain,
    match_account,
    resolve_accounts,
)
from nexusvenue.evals.resolution import degrade_contacts, evaluate_resolution
from nexusvenue.mockdata.generate import generate, mutate_delta


def acct(aid, name, industry="Technology"):
    return {"account_id": aid, "account_name": name, "industry": industry}


def contact(cid, aid, email):
    return {"contact_id": cid, "account_id": aid, "email": email, "full_name": "Pat Doe",
            "title": "Events Director", "agency_id": None}


def names(canonical):
    return sorted(tuple(c["aliases"]) for c in canonical)


# --------------------------------------------------------------------------
# evidence helpers
# --------------------------------------------------------------------------

def test_email_domain_ignores_free_mail_and_junk():
    assert email_domain("Kim@Luminark.com") == "luminark.com"
    assert email_domain("kim@gmail.com") is None
    assert email_domain("kim@mail.luminark.com") == "mail.luminark.com"  # subdomains kept: errs toward not merging
    assert email_domain(None) is None and email_domain("no-at-sign") is None


def test_dominant_domain_needs_a_strict_majority():
    rows = [acct("A1", "Luminark")]
    one = [contact("C1", "A1", "a@luminark.com")]
    assert account_domains(rows, one) == {"A1": "luminark.com"}
    split = one + [contact("C2", "A1", "b@other.com")]            # 1 vs 1: no dominant domain
    assert account_domains(rows, split) == {"A1": None}
    majority = split + [contact("C3", "A1", "c@luminark.com")]    # 2 vs 1
    assert account_domains(rows, majority) == {"A1": "luminark.com"}
    assert account_domains(rows, [contact("C1", "A1", "a@gmail.com")]) == {"A1": None}


# --------------------------------------------------------------------------
# what contact evidence adds, and what it must never do
# --------------------------------------------------------------------------

ROWS = [acct("ORL-1", "Luminark"), acct("MIA-2", "LMK"), acct("CHI-3", "Harborline Restaurants", "Hospitality")]
CONTACTS = [contact("C1", "ORL-1", "a@luminark.com"), contact("C2", "MIA-2", "b@luminark.com"),
            contact("C3", "CHI-3", "c@harborline.com")]


def test_abbreviations_connect_through_contacts_not_names():
    assert len(resolve_accounts(ROWS)) == 3                        # name-only: LMK is a stranger
    merged = resolve_accounts(ROWS, CONTACTS)
    assert names(merged) == [("Harborline Restaurants",), ("LMK", "Luminark")]
    assert next(c for c in merged if "LMK" in c["aliases"])["domain"] == "luminark.com"


def test_display_name_is_never_the_abbreviation():
    [c] = [c for c in resolve_accounts(ROWS, CONTACTS) if "LMK" in c["aliases"]]
    assert c["canonical_name"] == "Luminark"


def test_a_shared_domain_alone_is_not_enough():
    rows = [acct("A", "Luminark", "Technology"), acct("B", "LMK", "Insurance")]
    assert len(resolve_accounts(rows, [contact("C1", "A", "a@luminark.com"),
                                       contact("C2", "B", "b@luminark.com")])) == 2


def test_the_same_industry_alone_is_not_enough():
    rows = [acct("A", "Luminark"), acct("B", "Octavian")]
    assert len(resolve_accounts(rows, [contact("C1", "A", "a@luminark.com"),
                                       contact("C2", "B", "b@octavian.com")])) == 2


def test_free_mail_addresses_never_link_accounts():
    rows = [acct("A", "Luminark"), acct("B", "Octavian")]
    assert len(resolve_accounts(rows, [contact("C1", "A", "a@gmail.com"),
                                       contact("C2", "B", "b@gmail.com")])) == 2


def test_a_stray_contact_cannot_merge_two_companies():
    # "Ashcombe Inc" matches Ashcombe by name, but its only contact is filed under
    # Calder & Voss's domain. Name evidence wins; the stray contact is outvoted.
    rows = [acct("C-1", "Calder & Voss", "Professional Services"),
            acct("C-2", "Calder & Voss LLP", "Professional Services"),
            acct("A-1", "Ashcombe", "Professional Services"),
            acct("A-2", "Ashcombe PLC", "Professional Services"),
            acct("A-3", "Ashcombe Federal Services", "Professional Services"),
            acct("A-4", "Ashcombe Incorporated", "Professional Services")]
    contacts = [contact("k1", "C-1", "x@calderandvoss.com"), contact("k2", "C-2", "y@calderandvoss.com"),
                contact("k3", "A-1", "a@ashcombe.com"), contact("k4", "A-2", "b@ashcombe.com"),
                contact("k5", "A-3", "c@ashcombe.com"),
                contact("k6", "A-4", "x@calderandvoss.com")]     # the stray
    merged = resolve_accounts(rows, contacts)
    assert len(merged) == 2
    assert not any("Calder & Voss" in c["aliases"] and "Ashcombe" in c["aliases"] for c in merged)
    assert {c["domain"] for c in merged} == {"calderandvoss.com", "ashcombe.com"}


def test_an_account_whose_contacts_disagree_has_no_contact_evidence():
    rows = [acct("A", "Luminark"), acct("B", "LMK")]
    contacts = [contact("C1", "A", "a@luminark.com"), contact("C2", "B", "b@luminark.com"),
                contact("C3", "B", "c@elsewhere.com")]            # B: 1 vs 1 -> no dominant domain
    assert len(resolve_accounts(rows, contacts)) == 2


@pytest.mark.parametrize("variants, expected", [
    (["Luminark", "Luminark Cloud, Inc.", "LMK"], "Luminark"),
    (["Aerlight Airways", "Aerlight Airways Holdings", "ALW"], "Aerlight Airways"),
    (["Calder & Voss", "Calder & Voss LLP", "CALDER & VOSS ADVISORY LLP"], "Calder & Voss"),
    (["KESTREL-DYNAMICS", "Kestrel Dynamics", "Kestrel Dynamics Corp."], "Kestrel Dynamics"),
    (["Wexford Markets Inc", "Wexford Markets"], "Wexford Markets"),
])
def test_display_name(variants, expected):
    assert choose_display_name(variants) == expected
    assert choose_display_name(list(reversed(variants))) == expected    # order never matters


# --------------------------------------------------------------------------
# measured against the generator's ground truth
# --------------------------------------------------------------------------

@pytest.fixture(scope="module", params=["acme", "globex"])
def crm(request, tmp_path_factory):
    d = tmp_path_factory.mktemp(request.param)
    generate(out_db=d / "crm.db", goldset_path=d / "g.json", company_flavor=request.param)
    return extract(db_path=d / "crm.db")


def test_contact_evidence_lifts_recall_without_a_single_false_merge(crm):
    baseline = evaluate_resolution(crm, use_contacts=False)
    now = evaluate_resolution(crm)
    assert baseline["pairwise_precision"] == now["pairwise_precision"] == 1.0
    assert baseline["pairwise_recall"] < 0.5
    assert now["pairwise_recall"] >= 0.99
    assert now["accounts_canonical"] == now["truth_companies"]


@pytest.mark.parametrize("fraction", [0.2, 0.4, 0.6, 1.0])
def test_noisy_contacts_cost_recall_never_precision(crm, fraction):
    baseline = evaluate_resolution(crm, use_contacts=False)
    noisy = evaluate_resolution(degrade_contacts(crm, fraction))
    assert noisy["pairwise_precision"] == 1.0 and noisy["false_merges"] == 0
    assert noisy["pairwise_recall"] >= baseline["pairwise_recall"]   # never worse than names alone
    if fraction == 1.0:                                              # no usable contacts: exactly the baseline
        assert noisy["pairwise_recall"] == baseline["pairwise_recall"]


def test_a_batch_rebuild_after_the_delta_keeps_ashcombe_and_calder_apart(tmp_path):
    # mutate_delta files a Calder & Voss person under "Ashcombe Incorporated": the
    # stray-contact case. A full rebuild must still not merge the two companies.
    generate(out_db=tmp_path / "crm.db", goldset_path=tmp_path / "g.json")
    mutate_delta(db_path=tmp_path / "crm.db")
    raw = extract(db_path=tmp_path / "crm.db")
    merged = resolve_accounts(raw["accounts"], raw["contacts"])
    ashcombe = next(c for c in merged if "Ashcombe Incorporated" in c["aliases"])
    assert all("Calder" not in alias for alias in ashcombe["aliases"])
    assert len(merged) == 21                       # 20 companies + Brightledger
    assert evaluate_resolution(raw)["false_merges"] == 0


# --------------------------------------------------------------------------
# incremental sync applies the same rules
# --------------------------------------------------------------------------

def existing(**kw):
    base = {"id": "ACCT-0001", "name": "Luminark", "industry": "Technology", "domain": "luminark.com",
            "aliases": ["Luminark"], "source_ids": ["ORL-ACC1"]}
    base.update(kw)
    return base


def delta(accounts, contacts):
    return {"accounts": accounts, "agencies": [], "contacts": contacts, "rfps": [], "beo_history": []}


def test_match_account_name_beats_contact_evidence():
    pool = [existing(), existing(id="ACCT-0002", name="Calder & Voss", aliases=["Calder & Voss"],
                                  domain="calderandvoss.com", industry="Professional Services")]
    # the name says Luminark; the contact domain says Calder & Voss; the name wins
    assert match_account("Luminark Inc", pool, domain="calderandvoss.com",
                         industry="Professional Services") == "ACCT-0001"


def test_sync_connects_an_abbreviation_through_its_contacts():
    plan = L._resolve_delta(
        delta([acct("MIA-ACC9", "LMK")], [contact("MIA-CON9", "MIA-ACC9", "dana@luminark.com")]),
        [existing()], [])
    assert not plan["new_accounts"]
    [merged] = plan["merged_accounts"]
    assert merged["canonical_id"] == "ACCT-0001"
    assert merged["canonical_name"] == "Luminark"            # never renamed to the abbreviation
    assert set(merged["aliases"]) == {"Luminark", "LMK"}
    assert plan["src_to_account"]["MIA-ACC9"] == "ACCT-0001"


def test_sync_name_match_outranks_a_stray_contact():
    pool = [existing(id="ACCT-0001", name="Ashcombe", aliases=["Ashcombe"], domain="ashcombe.com",
                     industry="Professional Services"),
            existing(id="ACCT-0002", name="Calder & Voss", aliases=["Calder & Voss"],
                     domain="calderandvoss.com", industry="Professional Services")]
    plan = L._resolve_delta(
        delta([acct("CHI-ACC9", "Ashcombe Incorporated", "Professional Services")],
              [contact("CHI-CON9", "CHI-ACC9", "x@calderandvoss.com")]),
        pool, [])
    [merged] = plan["merged_accounts"]
    assert merged["canonical_id"] == "ACCT-0001"


def test_sync_ambiguous_contact_evidence_mints_a_new_account():
    twins = [existing(), existing(id="ACCT-0002", name="Luminark Labs", aliases=["Luminark Labs"],
                                  source_ids=["CHI-ACC2"])]
    plan = L._resolve_delta(
        delta([acct("MIA-ACC9", "LMK")], [contact("MIA-CON9", "MIA-ACC9", "dana@luminark.com")]),
        twins, [])
    assert [a["canonical_id"] for a in plan["new_accounts"]] == ["ACCT-S001"]


def test_sync_a_new_account_carries_its_contact_domain():
    plan = L._resolve_delta(
        delta([acct("MIA-ACC9", "Brightledger, Inc.")], [contact("MIA-CON9", "MIA-ACC9", "dana@brightledger.com")]),
        [existing()], [])
    [new] = plan["new_accounts"]
    assert new["domain"] == "brightledger.com"


def test_sync_fills_a_missing_domain_but_never_overwrites_one():
    row = acct("CHI-ACC9", "Luminark Inc")                       # matches Luminark by name
    contacts = [contact("CHI-CON9", "CHI-ACC9", "dana@luminark-cloud.com")]
    filled = L._resolve_delta(delta([row], contacts), [existing(domain=None)], [])
    assert filled["merged_accounts"][0]["domain"] == "luminark-cloud.com"
    kept = L._resolve_delta(delta([row], contacts), [existing()], [])
    assert kept["merged_accounts"][0]["domain"] == "luminark.com"
