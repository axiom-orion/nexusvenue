"""Export real NexusVenue pipeline output to web/data.js for the static showcase.

Runs the repository's own code over the seeded mock CRM (every company in it is
fictional) -- no Neo4j and no API keys needed:

  REAL  entity resolution + merge report      etl.resolve, scored against the
        generator's ground truth (pairwise precision/recall, and what it misses)
  REAL  hash-embedding hybrid retrieval       rag.embed.HashEmbedder, replicated in
        process against the resolved graph exactly as rag/retrieve.py builds it
  REAL  the SQL-vs-Cypher "live results"      computed over the resolved data
  REAL  precision/recall@k                    same metric as evals/metrics.py
  REAL  one business day of incremental sync  mutate_delta -> etl.load._resolve_delta

Not generated here, and labelled as such on the page:
  - the Win Strategy Blueprint is a *representative sample* assembled from each
    RFP's real retrieved context and schema-checked against
    rag.advisor.WinStrategyBlueprint. It is not model output; `nexusvenue ask`
    writes the real one with Claude.
  - the judge panel example is the live disagreement recorded in the README.

Usage:
    python scripts/export_showcase_data.py           # writes web/data.js
    python scripts/export_showcase_data.py --check   # exit 1 if web/data.js is stale
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from nexusvenue.etl import load as L
from nexusvenue.etl.extract import extract
from nexusvenue.etl.resolve import FUZZ_THRESHOLD, resolve_accounts, resolve_contacts
from nexusvenue.graph.queries import SHOWCASE
from nexusvenue.mockdata.generate import (
    ACCOUNTS_BY_FLAVOR,
    DELTA_TS,
    SEED,
    T0,
    generate,
    mutate_delta,
)
from nexusvenue.rag.advisor import WinStrategyBlueprint
from nexusvenue.rag.embed import HashEmbedder

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "data.js"
README = ROOT / "README.md"
GOLDSET = ROOT / "data" / "goldset.json"

FLAVOR = "acme"
EMBED_DIM = 1536
K = 6
OPEN_STATUSES = ("Open", "Proposal Sent", "Negotiating")

NOTE = (
    "Entity resolution, retrieval, the SQL-vs-Cypher results, the retrieval eval and the sync "
    "walkthrough on this page are computed by running this repository's own code over a seeded "
    "mock CRM; every company in it is fictional. Retrieval here uses the offline hash embedding "
    "backend, a keyless stand-in for Gemini embeddings. The Win Strategy Blueprint is a "
    "representative sample assembled from the real retrieved context, not model output."
)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _money(n: float) -> str:
    return f"${n:,.0f}"


def _sentence(text: str) -> str:
    """End with exactly one period ("Brightledger, Inc." needs no second one)."""
    return text if text.endswith(".") else text + "."


# ---------------------------------------------------------------------------
# the resolved world: what etl/load.py puts in the graph
# ---------------------------------------------------------------------------

def build_world(raw: dict) -> dict:
    accounts = resolve_accounts(raw["accounts"])
    contacts = resolve_contacts(raw["contacts"])
    acct_by_src = {sid: a for a in accounts for sid in a["source_ids"]}
    plnr_by_src = {sid: c for c in contacts for sid in c["source_ids"]}
    acct_by_cid = {a["canonical_id"]: a for a in accounts}
    plnr_by_cid = {c["canonical_id"]: c for c in contacts}
    agency_name = {g["agency_id"]: g["agency_name"] for g in raw["agencies"]}

    beos = []
    for r in raw["beo_history"]:
        acct = acct_by_src[r["account_id"]]
        plnr = plnr_by_src.get(r["contact_id"])
        ag_id = plnr["agency_id"] if plnr else None
        beos.append({
            "beo_id": r["beo_id"],
            "property_code": r["property_code"],
            "property": L.PROPERTY_NAMES[r["property_code"]][0],
            "event_type": r["event_type"],
            "event_date": r["event_date"],
            "attendees": r["attendee_count"],
            "fb_spend": r["fb_spend"],
            "av_spend": r["av_spend"],
            "total_revenue": r["total_revenue"],
            "ops_notes": r["ops_notes"],
            "account_cid": acct["canonical_id"],
            "account": acct["canonical_name"],
            "planner_cid": plnr["canonical_id"] if plnr else None,
            "planner": plnr["full_name"] if plnr else None,
            "agency_id": ag_id,
            "agency": agency_name.get(ag_id) if ag_id else None,
        })

    # REPRESENTS inference (etl/load.py REPRESENTS_INFER): an agency represents
    # an account if one of its planners booked an event for it
    represents: dict[str, set] = defaultdict(set)
    for b in beos:
        if b["agency_id"]:
            represents[b["agency_id"]].add(b["account_cid"])

    beos_by_account: dict[str, list] = defaultdict(list)
    for b in beos:
        beos_by_account[b["account_cid"]].append(b)

    return {
        "raw": raw, "accounts": accounts, "contacts": contacts,
        "acct_by_src": acct_by_src, "plnr_by_src": plnr_by_src,
        "acct_by_cid": acct_by_cid, "plnr_by_cid": plnr_by_cid,
        "agency_name": agency_name, "beos": beos,
        "represents": represents, "beos_by_account": beos_by_account,
    }


# ---------------------------------------------------------------------------
# entity resolution: the messy rows, the merges, and an honest score
# ---------------------------------------------------------------------------

def dirty_examples(w: dict) -> dict:
    raw = w["raw"]
    rows_by_cid: dict[str, list] = defaultdict(list)
    for r in raw["accounts"]:
        rows_by_cid[w["acct_by_src"][r["account_id"]]["canonical_id"]].append(r)
    accounts = []
    for cid in sorted(rows_by_cid):
        rows = rows_by_cid[cid]
        if len(rows) >= 3 and len({r["account_name"] for r in rows}) >= 3:
            a = w["acct_by_cid"][cid]
            accounts.append({
                "canonical": a["canonical_name"], "industry": a["industry"],
                "source_ids": a["source_ids"],
                "variants": [{"property": r["property_code"], "name": r["account_name"]} for r in rows],
            })
    contacts_rows: dict[str, list] = defaultdict(list)
    for r in raw["contacts"]:
        contacts_rows[w["plnr_by_src"][r["contact_id"]]["canonical_id"]].append(r)
    contacts = []
    for cid, rows in contacts_rows.items():
        names = {r["full_name"] for r in rows}
        if len(names) >= 2:
            c = w["plnr_by_cid"][cid]
            contacts.append((-len(names), cid, {
                "canonical": c["full_name"], "email": c["email"], "title": c["title"],
                "spellings": [{"property": r["property_code"], "name": r["full_name"]} for r in rows],
            }))
    contacts = [c[2] for c in sorted(contacts, key=lambda t: (t[0], t[1]))]
    return {"accounts": accounts[:4], "contacts": contacts[:4]}


def resolution_block(w: dict) -> dict:
    raw, accounts = w["raw"], w["accounts"]
    truth = {v: canonical for canonical, _, variants in ACCOUNTS_BY_FLAVOR[FLAVOR] for v in variants}
    cluster_of = {sid: a["canonical_id"] for a in accounts for sid in a["source_ids"]}
    rows = raw["accounts"]
    tp = fp = fn = 0
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            same_truth = truth[rows[i]["account_name"]] == truth[rows[j]["account_name"]]
            same_pred = cluster_of[rows[i]["account_id"]] == cluster_of[rows[j]["account_id"]]
            tp += same_truth and same_pred
            fp += (not same_truth) and same_pred
            fn += same_truth and (not same_pred)

    by_truth: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_truth[truth[r["account_name"]]][cluster_of[r["account_id"]]].append(r["account_name"])
    splits = sorted(
        ({"canonical": c, "groups": sorted(sorted(set(n)) for n in g.values())}
         for c, g in by_truth.items() if len(g) > 1),
        key=lambda s: (-len(s["groups"]), s["canonical"]),
    )

    merged = sorted((a for a in accounts if len(a["source_ids"]) > 1),
                    key=lambda a: (-len(a["aliases"]), a["canonical_id"]))
    return {
        "accounts_source": len(raw["accounts"]),
        "accounts_canonical": len(accounts),
        "accounts_merged": len(raw["accounts"]) - len(accounts),
        "contacts_source": len(raw["contacts"]),
        "contacts_canonical": len(w["contacts"]),
        "fuzz_threshold": FUZZ_THRESHOLD,
        "pairwise_precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "pairwise_recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "false_merges": fp,
        "clusters": [{"canonical": a["canonical_name"], "aliases": a["aliases"]} for a in merged[:8]],
        "misses": splits[:6],
        "miss_count": len(splits),
        "truth_companies": len(ACCOUNTS_BY_FLAVOR[FLAVOR]),
    }


# ---------------------------------------------------------------------------
# hybrid retrieval, replicated in-process (rag/retrieve.py without Neo4j)
# ---------------------------------------------------------------------------

class Retriever:
    def __init__(self, w: dict):
        self.w = w
        self.embedder = HashEmbedder(EMBED_DIM)
        self.vecs = self.embedder.embed([b["ops_notes"] for b in w["beos"]])

    def portfolio(self, cid: str) -> dict:
        a, evs = self.w["acct_by_cid"][cid], self.w["beos_by_account"][cid]
        return {
            "account": a["canonical_name"], "aliases": a["aliases"], "events": len(evs),
            "lifetime_revenue": round(sum(b["total_revenue"] for b in evs), 2),
            "avg_fb_spend": round(sum(b["fb_spend"] for b in evs) / len(evs), 2),
            "properties": sorted({b["property"] for b in evs}),
        }

    def agency_book(self, ag_id: str) -> dict:
        cids = self.w["represents"][ag_id]
        evs = [b for c in cids for b in self.w["beos_by_account"][c]]
        return {
            "agency": self.w["agency_name"][ag_id],
            "represented_accounts": sorted(self.w["acct_by_cid"][c]["canonical_name"] for c in cids),
            "total_events": len(evs),
            "total_revenue": round(sum(b["total_revenue"] for b in evs), 2),
        }

    def ranked(self, query: str) -> list[tuple[float, dict]]:
        """Every BEO, best first. Neo4j's cosine vector index reports (1 + cos) / 2.
        Equal scores are broken by BEO id so the export is deterministic; the live
        index breaks them arbitrarily."""
        qv = self.embedder.embed([query], task="RETRIEVAL_QUERY")[0]
        return sorted(((0.5 * (1 + _dot(qv, v)), b) for v, b in zip(self.vecs, self.w["beos"])),
                      key=lambda t: (-round(t[0], 9), t[1]["beo_id"]))

    def retrieve(self, query: str, k: int = K) -> dict:
        scored = self.ranked(query)[:k]
        hits = [{
            "score": round(s, 4), "beo_id": b["beo_id"], "event_type": b["event_type"],
            "event_date": b["event_date"], "attendees": b["attendees"], "fb_spend": b["fb_spend"],
            "av_spend": b["av_spend"], "total_revenue": b["total_revenue"], "ops_notes": b["ops_notes"],
            "account": b["account"], "property": b["property"], "planner": b["planner"],
            "agency": b["agency"],
        } for s, b in scored]
        cids = sorted({b["account_cid"] for _, b in scored})
        ag_ids = sorted({b["agency_id"] for _, b in scored if b["agency_id"]},
                        key=lambda g: self.w["agency_name"][g])
        return {
            "query": query, "similar_past_events": hits,
            "account_portfolios": [self.portfolio(c) for c in cids],
            "agency_relationships": [self.agency_book(g) for g in ag_ids],
        }


def eval_block(retriever: Retriever, goldset: dict) -> dict:
    rows = []
    for key, spec in goldset.items():
        relevant = set(spec["relevant_beo_ids"])
        ranking = retriever.ranked(spec["query"])
        got = [b["beo_id"] for _, b in ranking[:K]]
        tp = len(set(got) & relevant)
        rows.append({
            "query_key": key, "relevant": len(relevant), "retrieved": len(got), "true_positives": tp,
            f"precision@{K}": round(tp / len(got), 3), f"recall@{K}": round(tp / len(relevant), 3),
            # a score tie straddling the cutoff: which event makes the top k is arbitrary
            "tie_at_cutoff": round(ranking[K - 1][0], 9) == round(ranking[K][0], 9),
        })
    n = len(rows)
    return {
        "k": K, "queries": rows,
        "macro_precision": round(sum(r[f"precision@{K}"] for r in rows) / n, 3),
        "macro_recall": round(sum(r[f"recall@{K}"] for r in rows) / n, 3),
    }


def readme_measured() -> dict | None:
    """The measured hash-vs-Gemini numbers from the README, if it still states them."""
    m = re.search(
        r"hash backend\s+\*\*P@6\s+([\d.]+)\s*/\s*R@6\s+([\d.]+)\*\*\s*→\s*Gemini embeddings\s+"
        r"\*\*P@6\s+([\d.]+)\s*/\s*R@6\s+([\d.]+)\*\*", README.read_text(encoding="utf-8"))
    if not m:
        print("note: README no longer states the hash/Gemini eval numbers; omitting them", file=sys.stderr)
        return None
    p_h, r_h, p_g, r_g = map(float, m.groups())
    return {"hash": {"precision": p_h, "recall": r_h}, "gemini": {"precision": p_g, "recall": r_g}}


# ---------------------------------------------------------------------------
# the representative blueprint (template over real retrieved context)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def _best_sentence(notes: str, query: str) -> str:
    sentences = [s.strip() for s in re.split(r"(?<=[.;])\s+", notes) if s.strip()] or [notes]
    q = _tokens(query)
    return max(sentences, key=lambda s: len(_tokens(s) & q)).rstrip(".;")


def sample_blueprint(query: str, ctx: dict) -> dict:
    hits = ctx["similar_past_events"]
    n, top = len(hits), hits[0]
    ports = sorted(ctx["account_portfolios"], key=lambda p: (-p["events"], p["account"]))
    agencies = sorted(ctx["agency_relationships"], key=lambda a: (-a["total_revenue"], a["agency"]))
    acct = ports[0] if ports else None
    agency = agencies[0] if agencies else None
    avg_fb = sum(h["fb_spend"] for h in hits) / n
    avg_total = sum(h["total_revenue"] for h in hits) / n
    peak = max(hits, key=lambda h: h["total_revenue"])

    factors = [f"Strong precedent: {n} semantically similar past events retrieved, led by "
               f"{top['event_type']} at {top['property']} (beo {top['beo_id']})."]
    if acct:
        sites = len(acct["properties"])
        history = (f"is an established multi-event account: {acct['events']} events, "
                   f"{_money(acct['lifetime_revenue'])} lifetime revenue across {sites} "
                   f"propert{'y' if sites == 1 else 'ies'}." if acct["events"] > 1 else
                   f"has booked with us before ({_money(acct['lifetime_revenue'])} lifetime revenue).")
        factors.append(f"{acct['account']} {history}")
    if agency:
        factors.append(f"Warm agency path via {agency['agency']}: already represents "
                       f"{len(agency['represented_accounts'])} of our accounts "
                       f"({_money(agency['total_revenue'])} booked).")

    contacts = []
    if top["planner"]:
        contacts.append({"name": top["planner"], "role": "planner",
                         "why": f"Booked {top['beo_id']}, the closest comparable to this RFP."})
    if agency:
        contacts.append({"name": agency["agency"], "role": "agency",
                         "why": "Existing relationship across multiple accounts: a warm intro path."})

    steps = []
    if agency:
        steps.append(f"Warm-intro through {agency['agency']} within 48h.")
    steps += [f"Build the proposal around beo {top['beo_id']} as the reference execution.",
              "Price the base package to the retrieved comparables' spend band; prepare the upgrade tier."]

    blueprint = {
        "executive_summary": (
            f"This RFP maps cleanly onto proven demand: retrieval surfaced {n} comparable executions"
            + (f", and {acct['account']} already has booked history in our portfolio" if acct else "")
            + ". Lead with the operational strengths our comparables were praised for and anchor "
              "pricing to their spend."),
        "win_probability_factors": factors,
        "recommended_packages": [
            {"name": f"Signature {top['event_type']} Package",
             "description": "Anchored on the operational elements our retrieved comparables were "
                            "praised for, scaled to the RFP's headcount.",
             "pricing_guidance": f"Historical comparables averaged {_money(avg_fb)} F&B and "
                                 f"{_money(avg_total)} total revenue; price the base package in that band.",
             "supporting_beo_ids": [h["beo_id"] for h in hits[:3]]},
            {"name": "White-Glove Experience Upgrade",
             "description": "Premium add-on tier (bespoke receptions, VIP logistics, broadcast AV) "
                            "matching the highest-spend comparable in the retrieved set.",
             "pricing_guidance": f"Upsell 20-35% above the base, consistent with the top comparable "
                                 f"({peak['beo_id']}, {_money(peak['total_revenue'])}).",
             "supporting_beo_ids": [peak["beo_id"]]},
        ],
        "historical_evidence": [
            {"beo_id": h["beo_id"],
             "insight": f"{_best_sentence(h['ops_notes'], query)} — {h['event_type']} for "
                        f"{h['attendees']} at {h['property']}."}
            for h in hits[:3]],
        "key_contacts": contacts,
        "operational_risks": [
            "Comparable spend varies widely; confirm this client's budget band before quoting.",
            "Signature operational elements (specialty catering / AV builds) carry long lead times; "
            "lock vendors early."],
        "next_steps": steps,
    }
    WinStrategyBlueprint.model_validate(blueprint)           # must conform to the real schema
    cited = {b for p in blueprint["recommended_packages"] for b in p["supporting_beo_ids"]} | \
            {e["beo_id"] for e in blueprint["historical_evidence"]}
    assert cited <= {h["beo_id"] for h in hits}, "blueprint cites a BEO that was not retrieved"
    return blueprint


# ---------------------------------------------------------------------------
# SQL vs Cypher: live results over the resolved data
# ---------------------------------------------------------------------------

def showcase_results(w: dict) -> list[dict]:
    raw, beos, represents = w["raw"], w["beos"], w["represents"]

    warm = []
    for r in raw["rfps"]:
        p = w["plnr_by_src"].get(r["contact_id"])
        if r["status"] not in OPEN_STATUSES or not p or not p["agency_id"]:
            continue
        for cid in represents[p["agency_id"]]:
            for b in w["beos_by_account"][cid]:
                if b["fb_spend"] > 50000:
                    warm.append({"rfp": r["rfp_id"], "planner": p["full_name"],
                                 "agency": w["agency_name"][p["agency_id"]],
                                 "proven_account": w["acct_by_cid"][cid]["canonical_name"],
                                 "fb_spend": round(b["fb_spend"], 2)})
    warm.sort(key=lambda x: (-x["fb_spend"], x["rfp"], x["proven_account"]))

    whales = []
    for cid, evs in w["beos_by_account"].items():
        props = sorted({b["property_code"] for b in evs})
        if len(props) > 1:
            a = w["acct_by_cid"][cid]
            whales.append({"account": a["canonical_name"], "crm_name_variants": a["aliases"],
                           "props": props,
                           "lifetime_revenue": round(sum(b["total_revenue"] for b in evs), 2)})
    whales.sort(key=lambda x: (-x["lifetime_revenue"], x["account"]))

    controlled: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for b in beos:
        if b["planner_cid"]:
            controlled[b["planner_cid"]][0] += 1
            controlled[b["planner_cid"]][1] += b["total_revenue"]
    influence = []
    for pid, (events, revenue) in controlled.items():
        p = w["plnr_by_cid"][pid]
        influence.append({"planner": p["full_name"], "agency": w["agency_name"].get(p["agency_id"]),
                          "events": events, "controlled_revenue": round(revenue, 2)})
    influence.sort(key=lambda x: (-x["controlled_revenue"], x["planner"]))

    results = {
        "agency_warm_paths": (
            ["rfp", "planner", "agency", "proven_account", "fb_spend"], warm[:10],
            "A 5-JOIN junction chain collapses into one readable path pattern, and the REPRESENTS "
            "edge it walks was inferred from booking history, not typed in by anyone."),
        "cross_property_whales": (
            ["account", "crm_name_variants", "props", "lifetime_revenue"], whales[:10],
            "SQL has nothing to GROUP BY: each property filed the same company under a different "
            "name. One resolved Account node turns cross-property spend into a single aggregation."),
        "planner_influence": (
            ["planner", "agency", "events", "controlled_revenue"], influence[:10],
            "Aggregating over the resolved Planner node counts a person once, however many "
            "properties spelled their name differently."),
    }
    out = []
    for item in SHOWCASE:
        columns, rows, takeaway = results[item["name"]]
        out.append({"name": item["name"], "question": item["question"], "sql": item["sql"],
                    "cypher": item["cypher"], "takeaway": takeaway, "columns": columns, "results": rows})
    return out


def graph_example(w: dict) -> dict:
    """A real multi-property account and one real path through it, for the diagram."""
    candidates = []
    for cid, evs in w["beos_by_account"].items():
        if len({b["property_code"] for b in evs}) > 1:
            candidates.append((-sum(b["total_revenue"] for b in evs), cid))
    candidates.sort()
    chosen = None
    for _, cid in candidates:  # prefer an account with an agency-booked event: it shows the whole path
        if any(b["agency_id"] for b in w["beos_by_account"][cid]):
            chosen = cid
            break
    chosen = chosen or candidates[0][1]
    a, evs = w["acct_by_cid"][chosen], w["beos_by_account"][chosen]
    agency_events = sorted((b for b in evs if b["agency_id"]), key=lambda b: -b["total_revenue"])
    first = agency_events[0] if agency_events else max(evs, key=lambda b: b["total_revenue"])
    others = sorted((b for b in evs if b is not first and b["property_code"] != first["property_code"]),
                    key=lambda b: -b["total_revenue"]) or \
        sorted((b for b in evs if b is not first), key=lambda b: -b["total_revenue"])
    shown = [first] + others[:1]
    return {
        "account": a["canonical_name"], "aliases": a["aliases"],
        "properties": sorted({b["property_code"] for b in evs}),
        "lifetime_revenue": round(sum(b["total_revenue"] for b in evs), 2),
        "planner": first["planner"], "agency": first["agency"],
        "beos": [{"id": b["beo_id"], "event_type": b["event_type"], "property": b["property_code"],
                  "total_revenue": b["total_revenue"]} for b in shown],
    }


# ---------------------------------------------------------------------------
# incremental sync: one real business day through the real resolution code
# ---------------------------------------------------------------------------

def delta_block(db: Path, full_raw: dict, w: dict) -> dict:
    day = db.with_name("crm_delta.db")
    shutil.copy(db, day)
    mutate_delta(db_path=day)

    boundary = L._boundary_ids(full_raw, T0)
    raw_all = extract(db_path=day, since=T0)
    raw = L._drop_boundary(raw_all, T0, boundary)
    changed = {t: len(rows) for t, rows in raw.items() if rows}

    graph_accounts = [{"id": a["canonical_id"], "name": a["canonical_name"], "industry": a["industry"],
                       "aliases": a["aliases"], "source_ids": a["source_ids"]} for a in w["accounts"]]
    graph_planners = [{"id": c["canonical_id"], "email": c["email"], "name": c["full_name"],
                       "title": c["title"], "agency_id": c["agency_id"], "source_ids": c["source_ids"]}
                      for c in w["contacts"]]
    plan = L._resolve_delta(raw, copy.deepcopy(graph_accounts), copy.deepcopy(graph_planners))

    new_wm = L._max_modified(raw)
    boundary2 = L._boundary_ids(raw_all, new_wm)
    rerun = L._drop_boundary(extract(db_path=day, since=new_wm), new_wm, boundary2)
    assert not any(rerun.values()), "a second sync must be a no-op"

    names = {r["account_id"]: r["account_name"] for r in full_raw["accounts"] + raw["accounts"]}
    old = {a["id"]: a for a in graph_accounts}
    agency_name = w["agency_name"]
    plnr_after = {p["canonical_id"]: p for p in plan["planners"]}
    full_rfp_status = {r["rfp_id"]: r["status"] for r in full_raw["rfps"]}
    old_plnr_by_src = {sid: p for p in graph_planners for sid in p["source_ids"]}

    changes = []
    merged_ids = {a["canonical_id"] for a in plan["merged_accounts"]}
    new_ids = {a["canonical_id"] for a in plan["new_accounts"]}
    for r in raw["accounts"]:
        cid = plan["src_to_account"][r["account_id"]]
        if cid in merged_ids:
            before = old[cid]
            changes.append({"path": "New name variant, existing account", "detail": (
                f"'{r['account_name']}' ({r['property_code']}) is variant #{len(before['aliases']) + 1} "
                f"of {before['name']}; incremental entity resolution merges it into the existing "
                f"canonical node instead of forking a duplicate.")})
        elif cid in new_ids:
            changes.append({"path": "Brand-new account", "detail": (
                f"'{r['account_name']}' ({r['property_code']}) has no fuzzy match to any canonical "
                f"account, so a new Account node is minted ({cid}).")})
    new_planner_ids = {p["canonical_id"] for p in plan["new_planners"]}
    for r in raw["contacts"]:
        pid = plan["src_to_planner"][r["contact_id"]]
        if pid in new_planner_ids:
            changes.append({"path": "New contact", "detail": (
                _sentence(f"{r['full_name']}, {r['email']}, {r['title']} at {names[r['account_id']]}"))})
        else:
            before = old_plnr_by_src.get(r["contact_id"]) or next(
                p for p in graph_planners if p["id"] == pid)
            changes.append({"path": "Duplicate-person drift", "detail": (
                f"{before['name']} re-entered as '{r['full_name']}' ({r['property_code']}) with the "
                f"same email; the row collapses on the email key, no duplicate planner.")})
    for r in raw["beo_history"]:
        cid = plan["src_to_account"][r["account_id"]]
        plnr = plan["src_to_planner"].get(r["contact_id"])
        ag = next((p["agency_id"] for p in graph_planners if p["id"] == plnr), None) or \
            (plnr_after.get(plnr) or {}).get("agency_id")
        via = f" via {agency_name[ag]}" if ag else ""
        label = "New BEO (new account)" if cid in new_ids else "New BEO (existing account" + \
            (", via agency)" if ag else ")")
        note = _best_sentence(r["ops_notes"], r["ops_notes"].split(";")[0])
        changes.append({"path": label, "detail": (
            f"{r['event_type']}, {r['attendee_count']} attendees, {_money(r['total_revenue'])} total{via}: "
            f"'{note}.' " + ("The REPRESENTS edge is re-inferred." if ag else "Full new-entity path."))})
    for r in raw["rfps"]:
        if r["rfp_id"] in full_rfp_status:
            changes.append({"path": "RFP status update", "detail": (
                f"{r['rfp_id']} moved {full_rfp_status[r['rfp_id']]} → {r['status']}; "
                f"an update to an existing node, and the watermark advances.")})
        else:
            changes.append({"path": "New RFP", "detail": _sentence(
                f"{r['rfp_id']}: {r['event_type']} for {r['attendee_count']} attendees "
                f"({r['status']}) from {names[r['account_id']]}")})

    report = [
        f"delta rows since {T0}: " + ", ".join(f"{t}={n}" for t, n in changed.items()),
        f"accounts: {plan['account_rows_merged']} merged into existing canonicals, "
        f"{len(plan['new_accounts'])} new",
        f"planners: {plan['planner_rows_merged']} merged on email, {len(plan['new_planners'])} new",
    ]
    report += [f"  merged -> {a['canonical_id']}: aliases now {a['aliases']}" for a in plan["merged_accounts"]]
    report += [f"watermark advanced: {T0} -> {new_wm}", "",
               f"$ nexusvenue sync   # run again, nothing changed",
               f"up to date (watermark {new_wm}, no source changes)"]
    return {"changes": changes, "report": report, "watermark": f"{T0} -> {new_wm}",
            "idempotent": ("Re-running sync with no source changes is a no-op: the watermark advances "
                           "only after a successful upsert, and a boundary-id guard stops rows stamped "
                           "exactly at the watermark from being processed twice.")}


# ---------------------------------------------------------------------------

def judge_example() -> dict:
    """The live disagreement the README documents; qualitative on purpose."""
    return {
        "finding": ("The advisor wrote “five” mocktail events where the retrieved context "
                    "documents four."),
        "panel": [
            {"family": "Claude", "caught": True, "material": True, "passed": False},
            {"family": "Gemini", "caught": True, "material": False, "passed": True},
            {"family": "Grok", "caught": True, "material": False, "passed": True},
        ],
        "source": "A single live run, recorded in the README. Not an average, and not re-run for this page.",
        "rubric": [
            {"name": "Context precision", "rule": "≥ 0.90", "detail":
             "Fraction of factual claims fully supported by the retrieved context."},
            {"name": "Actionability", "rule": "≥ 3 of 5", "detail":
             "Whether next steps are concrete and executable, not generic advice."},
            {"name": "Material hallucinations", "rule": "none", "detail":
             "Claims the context does not support. The judge decides which are material."},
        ],
    }


def build() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        db, gold = tmp / "crm.db", tmp / "goldset.json"
        counts = generate(out_db=db, goldset_path=gold, company_flavor=FLAVOR)
        goldset = json.loads(gold.read_text())
        if GOLDSET.exists() and json.loads(GOLDSET.read_text()) != goldset:
            print("warning: data/goldset.json differs from the generator's output", file=sys.stderr)

        raw = extract(db_path=db)
        w = build_world(raw)
        retriever = Retriever(w)
        rfps = []
        for key, spec in goldset.items():
            ctx = retriever.retrieve(spec["query"])
            rfps.append({"key": key, "query": spec["query"], "context": ctx,
                         "blueprint": sample_blueprint(spec["query"], ctx)})

        return {
            "meta": {"generated_by": "scripts/export_showcase_data.py", "embed_backend": "hash",
                     "embed_dim": EMBED_DIM, "dataset": f"seeded mock CRM, flavor '{FLAVOR}', seed {SEED}",
                     "note": NOTE},
            "counts": counts,
            "properties": [{"code": c, "name": n, "city": city} for c, (n, city) in L.PROPERTY_NAMES.items()],
            "dirty": dirty_examples(w),
            "resolution": resolution_block(w),
            "graph_model": {
                "nodes": ["Account", "Planner", "Agency", "Property", "BEO", "RFP", "SyncState"],
                "relationships": [
                    "(Account)-[:EXECUTED]->(BEO)", "(BEO)-[:AT_PROPERTY]->(Property)",
                    "(BEO)-[:BOOKED_BY]->(Planner)", "(Planner)-[:EMPLOYED_BY]->(Agency)",
                    "(Agency)-[:REPRESENTS]->(Account)", "(Planner)-[:MANAGES]->(RFP)",
                    "(RFP)-[:FROM_ACCOUNT]->(Account)", "(RFP)-[:FOR_PROPERTY]->(Property)"],
            },
            "graph_example": graph_example(w),
            "sql_cypher": showcase_results(w),
            "rfps": rfps,
            "eval": {**eval_block(retriever, goldset), "measured": readme_measured()},
            "judge": judge_example(),
            "delta": delta_block(db, raw, w),
            "_gold": {k: v["relevant_beo_ids"] for k, v in goldset.items()},
        }


def render(data: dict) -> str:
    return ("// Generated by scripts/export_showcase_data.py. Do not edit by hand.\n"
            "window.NEXUS_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if web/data.js is out of date")
    args = ap.parse_args()
    text = render(build())
    if args.check:
        if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
            print(f"{OUT.relative_to(ROOT)} is up to date")
            return 0
        print(f"{OUT.relative_to(ROOT)} is stale: run `python scripts/export_showcase_data.py`", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
