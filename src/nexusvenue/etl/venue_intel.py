"""Venue-intelligence ETL — load the BAI venue/market graph into Neo4j.

Adds the market-intelligence layer the CRM graph lacks: real properties enriched
with archetype/tier/capacity, the OVERFLOW network (a room block flows across a
cluster, not one hotel), and the market housing tiers a citywide fills.

Graph additions:
  (:Property)                      enriched: archetype, catering_tier, event_sqft,
                                   guest_rooms, largest_ballroom, cluster
  (:Property)-[:OVERFLOWS]->(:Property)   compound|walkway|skybridge|shuttle|sister-brand|…
  (:Room)-[:IN_PROPERTY]->(:Property)     Tier-2 ballrooms with capacities
  (:Property)-[:IN_TIER]->(:MarketTier)-[:IN_MARKET]->(:Market)

Source CSVs live in data/venue/ (copied from the BAI venue graph).
Load is idempotent (MERGE-only), so it composes with the CRM `etl`/`sync`.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from neo4j import Driver

from nexusvenue.graph.schema import get_driver

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "venue"

# Orlando market housing tiers (mirrors @bai/contracts ORLANDO_MARKET). Member hotels
# for the two anchor tiers are matched by slug so the graph links properties -> tiers.
ORLANDO_TIERS = [
    {"name": "HQ + connected", "rooms": 5199, "reach": "walk", "ord": 1,
     "members": ["Hyatt Regency Orlando", "Hilton Orlando", "Rosen Centre Hotel", "Rosen Plaza Hotel"]},
    {"name": "Near-cluster (shuttle)", "rooms": 3061, "reach": "shuttle", "ord": 2,
     "members": ["Rosen Shingle Creek", "Renaissance Orlando at SeaWorld", "Hyatt Regency Grand Cypress"]},
    {"name": "I-Drive corridor", "rooms": 31740, "reach": "shuttle", "ord": 3, "members": []},
    {"name": "Greater Orlando", "rooms": 88000, "reach": "drive", "ord": 4, "members": []},
]


def slugify(name: str) -> str:
    """Stable code for a property: drop parentheticals, lowercase, hyphenate."""
    name = re.sub(r"\(.*?\)", "", name or "")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.strip().lower())).strip("-")


def _read_csv(path: Path) -> list[dict]:
    """Read a CSV, skipping blank lines and '#' comment lines (used in the edge file)."""
    with path.open(encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def parse_venue_intel(data_dir: Path = DATA_DIR) -> dict:
    """Parse the CSVs into graph-ready rows. Pure — no Neo4j. Powers --dry-run."""
    prop_rows = _read_csv(data_dir / "properties.csv")
    properties, code_index = [], {}
    for r in prop_rows:
        name = r["property"]
        code = slugify(name)
        code_index[code] = code
        properties.append({
            "code": code, "name": name, "market": r.get("market") or None,
            "archetype": r.get("proposed_archetype") or None,
            "event_sqft": _num(r.get("total_event_sqft")),
            "guest_rooms": _num(r.get("guest_rooms")),
            "largest_ballroom": _num(r.get("largest_ballroom_sqft")),
            "cluster": _cluster_of(r),
        })

    # rooms (Tier-2 ballrooms) — only for properties we know
    rooms = []
    for r in _read_csv(data_dir / "ballrooms.csv"):
        pcode = slugify(r["property"])
        if pcode not in code_index:
            continue
        rooms.append({
            "code": f"{pcode}/{slugify(r['room_name'])}", "property": pcode,
            "name": r["room_name"], "sqft": _num(r.get("sqft")),
            "cap_banquet": _num(r.get("cap_banquet")), "cap_reception": _num(r.get("cap_reception")),
        })

    # overflow edges — resolve endpoints; placeholders like "(neighbor hotels)" are skipped,
    # non-property anchors (convention centers, Disney Springs) become :Property{archetype:'anchor'}.
    edges, anchors, skipped = [], {}, []
    for r in _read_csv(data_dir / "overflow-edges.csv"):
        a, b = _resolve(r["property_a"], code_index, anchors), _resolve(r["property_b"], code_index, anchors)
        if a is None or b is None:
            skipped.append((r["property_a"], r["property_b"]))
            continue
        edges.append({
            "a": a, "b": b, "type": r.get("edge_type") or "neighbor",
            "direction": r.get("direction") or "bidirectional",
            "connection": r.get("connection") or None,
            "est_rooms": r.get("est_overflow_rooms") or None,
            "cluster": r.get("cluster") or None,
        })

    memberships = [
        {"property": slugify(m), "tier": t["name"]}
        for t in ORLANDO_TIERS for m in t["members"] if slugify(m) in code_index
    ]

    return {
        "properties": properties, "rooms": rooms, "edges": edges,
        "anchors": list(anchors.values()), "skipped": skipped,
        "tiers": ORLANDO_TIERS, "memberships": memberships,
    }


def _num(v):
    try:
        return int(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _cluster_of(row: dict) -> str | None:
    note = (row.get("notes") or "") + " " + (row.get("multiroom_connected_notes") or "")
    for c in ("OCCC", "Bonnet Creek", "EPCOT", "Grande Lakes", "Universal", "Epic Universe",
              "Dallas", "Nashville", "WDW"):
        if c.lower() in note.lower():
            return c
    return None


def _resolve(endpoint: str, code_index: dict, anchors: dict) -> str | None:
    ep = (endpoint or "").strip()
    if not ep or ep.startswith("("):  # placeholder like "(neighbor hotels)"
        return None
    code = slugify(ep)
    if code in code_index:
        return code
    # a known real anchor referenced by edges but not a property row
    if any(w in ep.lower() for w in ("convention center", "disney springs", "occc")):
        anchors[code] = {"code": code, "name": ep}
        return code
    return None


# ── Cypher ───────────────────────────────────────────────────────────────────
PROP_UPSERT = """
UNWIND $rows AS r
MERGE (p:Property {code: r.code})
SET p.name = r.name, p.market = r.market, p.archetype = r.archetype,
    p.event_sqft = r.event_sqft, p.guest_rooms = r.guest_rooms,
    p.largest_ballroom = r.largest_ballroom, p.cluster = r.cluster, p.venue_intel = true
"""
ANCHOR_UPSERT = """
UNWIND $rows AS r MERGE (p:Property {code: r.code}) SET p.name = r.name, p.archetype = 'anchor'
"""
ROOM_UPSERT = """
UNWIND $rows AS r
MERGE (rm:Room {code: r.code})
SET rm.name = r.name, rm.sqft = r.sqft, rm.cap_banquet = r.cap_banquet, rm.cap_reception = r.cap_reception
WITH rm, r MATCH (p:Property {code: r.property}) MERGE (rm)-[:IN_PROPERTY]->(p)
"""
EDGE_UPSERT = """
UNWIND $rows AS r
MATCH (a:Property {code: r.a}) MATCH (b:Property {code: r.b})
MERGE (a)-[o:OVERFLOWS {kind: r.type}]->(b)
SET o.direction = r.direction, o.connection = r.connection, o.est_rooms = r.est_rooms, o.cluster = r.cluster
"""
MARKET_UPSERT = """
MERGE (m:Market {name: $market})
WITH m UNWIND $tiers AS t
MERGE (ti:MarketTier {name: t.name})
SET ti.rooms = t.rooms, ti.reach = t.reach, ti.ord = t.ord
MERGE (ti)-[:IN_MARKET]->(m)
"""
MEMBERSHIP_UPSERT = """
UNWIND $rows AS r
MATCH (p:Property {code: r.property}) MATCH (ti:MarketTier {name: r.tier})
MERGE (p)-[:IN_TIER]->(ti)
"""


def load_venue_intel(driver: Driver | None = None, data_dir: Path = DATA_DIR) -> str:
    own = driver is None
    driver = driver or get_driver()
    d = parse_venue_intel(data_dir)
    with driver.session() as s:
        s.run(PROP_UPSERT, rows=d["properties"])
        if d["anchors"]:
            s.run(ANCHOR_UPSERT, rows=d["anchors"])
        if d["rooms"]:
            s.run(ROOM_UPSERT, rows=d["rooms"])
        s.run(EDGE_UPSERT, rows=d["edges"])
        s.run(MARKET_UPSERT, market="Orlando", tiers=[{k: t[k] for k in ("name", "rooms", "reach", "ord")} for t in d["tiers"]])
        s.run(MEMBERSHIP_UPSERT, rows=d["memberships"])
    if own:
        driver.close()
    return _summary(d)


def _summary(d: dict) -> str:
    return (
        f"venue intel loaded: {len(d['properties'])} properties, {len(d['rooms'])} rooms, "
        f"{len(d['edges'])} overflow edges, {len(d['anchors'])} anchors, "
        f"{len(d['memberships'])} tier memberships across {len(d['tiers'])} Orlando tiers"
        + (f"\nskipped {len(d['skipped'])} edges (unresolved endpoints): "
           + ", ".join(f"{a}~{b}" for a, b in d["skipped"][:6]) if d["skipped"] else "")
    )
