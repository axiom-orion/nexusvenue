"""Role/Concern intelligence — the altitude/lane/worry knowledge graph.

Adds the organizational layer the CRM+venue graphs lack: WHO operates at what
scope (Role, tagged by altitude and lane) and WHAT they're actually worried
about (Concern), connected by the real intersections found across two design
interviews with Ryan (BAI pipeline architecture) plus qualified industry
sources (Events Industry Council's CMP-IS, ACF, AHLEI, ADMEI/DMCP, and a
Wiley off-premise-catering textbook).

Graph additions:
  (:Role {id, title, altitude, lane, is_internal})
  (:Concern {id, description})            -- description gets embedded
  (:Role)-[:WORRIES_ABOUT {rank}]->(:Concern)
  (:Role)-[:ESCALATES_TO]->(:Role)         -- the real reporting chain
  (:Role)-[:HANDS_OFF_TO {trigger, worry}]->(:Role)   -- internal handoff, ack-engine eligible
  (:Role)-[:ROUTES_THROUGH]->(:Role)       -- external party -> its single internal contact

`lane` mirrors bai-contracts' coordination.ts Department where one exists
(sales, stewarding, culinary-hot/cold/pastry, banquet-captain/server/bar,
event-manager, client) and uses a descriptive superset where it doesn't
(peripheral, dmc, off-site-caterer — deliberately NOT ack-engine
departments; see ROUTES_THROUGH below for why DMC has no HANDS_OFF_TO edge).

Load is idempotent (MERGE-only), composes with the CRM etl/sync and the
venue-intel load, same as venue_intel.py.
"""

from __future__ import annotations

from neo4j import Driver

from nexusvenue.graph.schema import get_driver

# ── Roles ─────────────────────────────────────────────────────────────────
# altitude: "ground" | "5k" | "10k" | "25k" | "50k"
ROLES: list[dict] = [
    # Client/planning — external, no ack-engine lane, funnels through sales
    {"id": "client-first-time", "title": "First-time event planner", "altitude": "5k", "lane": "client", "is_internal": False},
    {"id": "client-wedding-planner", "title": "Professional wedding planner", "altitude": "5k", "lane": "client", "is_internal": False},
    # DMC — external, updates the hotel, routes through catering sales, no ack-engine lane
    {"id": "dmc-agent", "title": "Destination management agent (DMCP-track)", "altitude": "5k", "lane": "dmc", "is_internal": False},
    {"id": "dmc-principal", "title": "DMC agency principal", "altitude": "25k", "lane": "dmc", "is_internal": False},
    # Sales — internal, lane="sales" (matches coordination.ts exactly)
    {"id": "group-sales-rep", "title": "Group Sales rep", "altitude": "10k", "lane": "sales", "is_internal": True},
    {"id": "catering-sales-handler", "title": "Catering Sales handler", "altitude": "5k", "lane": "sales", "is_internal": True},
    {"id": "sales-director", "title": "Sales Director", "altitude": "25k", "lane": "sales", "is_internal": True},
    # Ops/coordination — lane="event-manager" (matches coordination.ts)
    {"id": "convention-services-manager", "title": "Convention Services Manager (CMP)", "altitude": "10k", "lane": "event-manager", "is_internal": True},
    {"id": "gm", "title": "General Manager", "altitude": "25k", "lane": "event-manager", "is_internal": True},
    {"id": "fb-director", "title": "Food & Beverage Director (CFBE)", "altitude": "25k", "lane": "event-manager", "is_internal": True},
    # Culinary — lane="culinary-hot" as the representative culinary lane (cold/pastry are peers)
    {"id": "executive-chef", "title": "Executive Chef (CEC)", "altitude": "10k", "lane": "culinary-hot", "is_internal": True},
    {"id": "head-banquet-chef", "title": "Head Banquet Chef (CSC-level)", "altitude": "5k", "lane": "culinary-hot", "is_internal": True},
    {"id": "line-cook", "title": "Line cook / prep", "altitude": "ground", "lane": "culinary-hot", "is_internal": True},
    # Stewarding
    {"id": "executive-steward", "title": "Executive Steward", "altitude": "10k", "lane": "stewarding", "is_internal": True},
    {"id": "steward-pull", "title": "Steward doing the pull", "altitude": "ground", "lane": "stewarding", "is_internal": True},
    # Banquet floor
    {"id": "banquet-manager", "title": "Banquet Manager", "altitude": "10k", "lane": "banquet-captain", "is_internal": True},
    {"id": "banquet-captain", "title": "Banquet Captain", "altitude": "5k", "lane": "banquet-captain", "is_internal": True},
    {"id": "server", "title": "Server / Bar / Houseman", "altitude": "ground", "lane": "server", "is_internal": True},
    # Peripheral — internal, but genuinely no coordination.ts lane yet (open question)
    {"id": "spa-manager", "title": "Spa Manager", "altitude": "10k", "lane": "peripheral", "is_internal": True},
    {"id": "spa-reservation-agent", "title": "Spa reservation agent", "altitude": "5k", "lane": "peripheral", "is_internal": True},
    # Off-site caterer — external, flattened altitude, no property safety net
    {"id": "off-site-caterer", "title": "Off-site/off-premise caterer", "altitude": "5k", "lane": "off-site-caterer", "is_internal": False},
]

# ── Concerns ──────────────────────────────────────────────────────────────
CONCERNS: list[dict] = [
    {"id": "not-living-up-to-vision", "description": "The event itself not living up to the vision the client had in mind, regardless of how smoothly the process ran."},
    {"id": "budget-surprise", "description": "A final bill that doesn't match what the client thought they agreed to -- hidden fees, service charges, minimums not being met."},
    {"id": "transport-desync", "description": "Transportation and ground logistics desyncing from the event schedule -- a session time shifts and nobody tells the DMC, so buses show up at the wrong time or sized for the wrong headcount."},
    {"id": "bad-client-experience-reflects-personally", "description": "A bad client experience reflecting on the sales director personally, even when the actual failure was operations', because the client associates the whole relationship with sales."},
    {"id": "group-cancels-or-shrinks-late", "description": "A group cancelling or shrinking late -- pipeline and forecast risk from space and dates held for a group that doesn't materialize as booked."},
    {"id": "team-drops-handoff", "description": "The sales team dropping the ball on handoff -- catering sales or ops missing something after the deal closes and the account moves to execution."},
    {"id": "change-too-late-to-execute", "description": "A change arriving too late to actually be executed well, regardless of whether the change itself was reasonable."},
    {"id": "conflicting-info-across-depts", "description": "Conflicting information across departments -- kitchen has one headcount, banquets has another, nobody is sure which is current."},
    {"id": "blamed-for-others-decision", "description": "Being blamed for a client-side or sales-side decision that operations or stewarding had no part in making, when it breaks during execution."},
    {"id": "staffing-cost-overrun", "description": "A change (more guests, added service) showing up without the staffing plan being adjusted to match, causing labor cost overruns."},
    {"id": "stale-version-pull", "description": "Pulling equipment against a stale or wrong version of the BEO -- a change happened after the pull sheet was generated and nobody caught it in time."},
    {"id": "blamed-for-shortage", "description": "Being the visible point of failure for an inventory shortage even when the root cause was upstream -- a late change, a bad ratio call, someone else's decision."},
    {"id": "walk-in-surprise", "description": "A peripheral department being surprised by a walk-in wave when a group's schedule shifts -- an activity cancels or ends early and a large group shows up with zero notice."},
    {"id": "overbook-stale-headcount", "description": "Staffing or appointments planned around a group size that changed after the peripheral department last heard about it."},
    {"id": "not-told-until-happening", "description": "Not being told anything at all until it's already happening, because the department is outside the core BEO loop by default."},
    {"id": "not-ready-on-time", "description": "Not being ready on time -- the single most universal execution failure across catering, stewarding, and kitchen operations at every level, hotel or off-premise."},
    {"id": "run-out-of-food", "description": "Running out of food or being unable to fulfill the contract as agreed -- the other half of the universal F&B execution anxiety, same at every level."},
    {"id": "no-safety-net", "description": "Being the entire F&B operation with no institutional layers to absorb a mistake -- unlike a hotel's F&B department, an off-premise caterer's failure has nowhere else to be caught."},
    {"id": "client-secret-doubt", "description": "The client secretly doubting the team is actually coordinated -- the real purpose a precon meeting's visible, hotel-heavy attendance is managing, not just information exchange."},
    {"id": "conflict-found-too-late", "description": "A double-booked space, staffing gap, or equipment conflict surfacing only after it's too late to fix quietly -- the reason internal BEO review meetings exist."},
    {"id": "relationship-ends-flat", "description": "Client goodwill decaying by default without a warm close -- even a flawlessly executed event can lose the rebooking if nobody closes the loop."},
]

# ── Role -> Concern (WORRIES_ABOUT, ranked) ──────────────────────────────
WORRIES: list[tuple[str, str, int]] = [
    ("client-first-time", "not-living-up-to-vision", 1),
    ("client-first-time", "budget-surprise", 2),
    ("client-wedding-planner", "not-living-up-to-vision", 1),
    ("client-wedding-planner", "budget-surprise", 2),
    ("dmc-agent", "transport-desync", 1),
    ("sales-director", "bad-client-experience-reflects-personally", 1),
    ("sales-director", "group-cancels-or-shrinks-late", 2),
    ("sales-director", "team-drops-handoff", 3),
    ("fb-director", "change-too-late-to-execute", 1),
    ("fb-director", "conflicting-info-across-depts", 2),
    ("fb-director", "blamed-for-others-decision", 3),
    ("fb-director", "staffing-cost-overrun", 4),
    ("executive-chef", "change-too-late-to-execute", 1),
    ("executive-chef", "conflicting-info-across-depts", 2),
    ("steward-pull", "stale-version-pull", 1),
    ("steward-pull", "blamed-for-shortage", 2),
    ("executive-steward", "blamed-for-shortage", 1),
    ("spa-reservation-agent", "walk-in-surprise", 1),
    ("spa-reservation-agent", "overbook-stale-headcount", 2),
    ("spa-reservation-agent", "not-told-until-happening", 3),
    ("spa-manager", "not-told-until-happening", 1),
    ("off-site-caterer", "not-ready-on-time", 1),
    ("off-site-caterer", "run-out-of-food", 2),
    ("off-site-caterer", "no-safety-net", 3),
    ("head-banquet-chef", "not-ready-on-time", 1),
    ("head-banquet-chef", "run-out-of-food", 2),
    ("catering-sales-handler", "client-secret-doubt", 1),
    ("catering-sales-handler", "relationship-ends-flat", 2),
    ("convention-services-manager", "client-secret-doubt", 1),
    ("convention-services-manager", "conflict-found-too-late", 2),
]

# ── Role -> Role: the real reporting chain (verified via ACF/AHLEI + primary-
#    source Marriott/Hyatt/Accor job descriptions, not guessed) ────────────
ESCALATES_TO: list[tuple[str, str]] = [
    ("steward-pull", "executive-steward"),
    ("executive-steward", "executive-chef"),  # confirmed: NOT an independent peer of culinary
    ("line-cook", "head-banquet-chef"),
    ("head-banquet-chef", "executive-chef"),  # CSC-level, reports to the CEC-level dept head
    ("executive-chef", "fb-director"),
    ("server", "banquet-captain"),
    ("banquet-captain", "banquet-manager"),
    ("banquet-manager", "fb-director"),
    ("catering-sales-handler", "sales-director"),
    ("group-sales-rep", "sales-director"),
    ("convention-services-manager", "gm"),
    ("sales-director", "gm"),
    ("fb-director", "gm"),
    ("spa-reservation-agent", "spa-manager"),
]

# ── Role -> Role: internal handoffs, the ones the ack engine actually routes
#    (trigger/worry match the intersection table from the altitude/lane map) ─
HANDS_OFF_TO: list[tuple[str, str, str, str]] = [
    ("catering-sales-handler", "convention-services-manager",
     "sales-led internal-review", "info arrives too late"),
    ("convention-services-manager", "executive-steward",
     "precon lock -> pull-sheet generation", "stale version, blamed for a shortage"),
]

# ── Role -> Role: external party's single internal point of contact. NOT an
#    ack-engine edge -- DMC/client/vendors are outside coordination.ts's
#    Department model entirely; catering sales absorbs the update and deals
#    with it, HITL, same funnel as any client-originated change. Ryan's own
#    correction (7/27): DMC usually updates the hotel, not the reverse, and
#    "the hotel just deals" with it -- there's no formal ack loop to build. ─
ROUTES_THROUGH: list[tuple[str, str]] = [
    ("client-first-time", "catering-sales-handler"),
    ("client-wedding-planner", "catering-sales-handler"),
    ("dmc-agent", "catering-sales-handler"),
]

# ── Cypher ───────────────────────────────────────────────────────────────────
ROLE_UPSERT = """
UNWIND $rows AS r
MERGE (role:Role {id: r.id})
SET role.title = r.title, role.altitude = r.altitude, role.lane = r.lane, role.is_internal = r.is_internal
"""
CONCERN_UPSERT = """
UNWIND $rows AS r
MERGE (c:Concern {id: r.id})
SET c.description = r.description
"""
WORRIES_UPSERT = """
UNWIND $rows AS r
MATCH (role:Role {id: r.role}) MATCH (c:Concern {id: r.concern})
MERGE (role)-[w:WORRIES_ABOUT]->(c)
SET w.rank = r.rank
"""
ESCALATES_UPSERT = """
UNWIND $rows AS r
MATCH (a:Role {id: r.from}) MATCH (b:Role {id: r.to})
MERGE (a)-[:ESCALATES_TO]->(b)
"""
HANDS_OFF_UPSERT = """
UNWIND $rows AS r
MATCH (a:Role {id: r.from}) MATCH (b:Role {id: r.to})
MERGE (a)-[h:HANDS_OFF_TO]->(b)
SET h.trigger = r.trigger, h.worry = r.worry
"""
ROUTES_UPSERT = """
UNWIND $rows AS r
MATCH (a:Role {id: r.from}) MATCH (b:Role {id: r.to})
MERGE (a)-[:ROUTES_THROUGH]->(b)
"""


def load_role_intel(driver: Driver | None = None) -> str:
    own = driver is None
    driver = driver or get_driver()
    with driver.session() as s:
        s.run(ROLE_UPSERT, rows=ROLES)
        s.run(CONCERN_UPSERT, rows=CONCERNS)
        s.run(WORRIES_UPSERT, rows=[{"role": r, "concern": c, "rank": k} for r, c, k in WORRIES])
        s.run(ESCALATES_UPSERT, rows=[{"from": a, "to": b} for a, b in ESCALATES_TO])
        s.run(HANDS_OFF_UPSERT, rows=[{"from": a, "to": b, "trigger": t, "worry": w} for a, b, t, w in HANDS_OFF_TO])
        s.run(ROUTES_UPSERT, rows=[{"from": a, "to": b} for a, b in ROUTES_THROUGH])
    if own:
        driver.close()
    return (
        f"role intel loaded: {len(ROLES)} roles, {len(CONCERNS)} concerns, "
        f"{len(WORRIES)} worries, {len(ESCALATES_TO)} escalation edges, "
        f"{len(HANDS_OFF_TO)} internal handoffs, {len(ROUTES_THROUGH)} external routing edges"
    )
