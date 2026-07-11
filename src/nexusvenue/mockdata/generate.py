"""Generate a realistically messy multi-property hospitality CRM in SQLite.

Simulates three property-level CRM orgs (Orlando, Miami, Chicago) that
were never integrated: the same corporate account appears under different
legal-name variants per property, contacts are duplicated with inconsistent
spellings, and unstructured BEO operational notes carry the institutional
knowledge that never made it into structured fields.

All companies are fictional; any resemblance to real firms is coincidental.

Also emits data/goldset.json — for a set of natural-language test queries, the
BEO ids whose ops_notes were deliberately seeded with matching content. This
is the ground truth for retrieval precision/recall evaluation.
"""

import json
import random
import sqlite3
from pathlib import Path

from nexusvenue.config import settings

SEED = 20260708

# Salesforce-style SystemModstamp analog. Full generate stamps everything at
# T0; mutate_delta() stamps a "next business day" batch after it, which is
# what the incremental sync watermark keys on.
T0 = "2026-07-01T00:00:00"
DELTA_TS = "2026-07-08T09:00:00"

PROPERTIES = [
    ("ORL", "NexusVenue Grand Orlando", "Orlando, FL", 1200),
    ("MIA", "NexusVenue Miami Beachfront", "Miami, FL", 800),
    ("CHI", "NexusVenue Chicago Riverside", "Chicago, IL", 950),
]

# Canonical corporate accounts with per-property name variants (the entity
# resolution challenge). (canonical, industry, [variants])
ACCOUNTS = [
    ("Calder & Voss", "Professional Services", ["Calder & Voss", "Calder & Voss LLP", "CALDER & VOSS ADVISORY LLP"]),
    ("Luminark", "Technology", ["Luminark", "Luminark Cloud, Inc.", "LMK"]),
    ("Bellwick", "Pharmaceutical", ["Bellwick", "Bellwick Inc.", "Bellwick Incorporated"]),
    ("Harrowgate Capital", "Financial Services", ["Harrowgate Capital", "H. Gate Capital", "Harrowgate Capital & Co"]),
    ("Kestrel Dynamics", "Aerospace & Defense", ["Kestrel Dynamics", "Kestrel Dynamics Corp.", "KESTREL-DYNAMICS"]),
    ("Hartley & Vale", "Consumer Goods", ["Hartley & Vale", "H&V", "Hartley and Vale Co"]),
    ("Ashcombe", "Professional Services", ["Ashcombe", "Ashcombe PLC", "Ashcombe Federal Services"]),
    ("Octavian", "Technology", ["Octavian", "Octavian Networks", "Octavian Networks, Inc."]),
    ("NovaCrest", "Pharmaceutical", ["NovaCrest", "NovaCrest Inc.", "Novacrest"]),
    ("Shieldstone Mutual", "Insurance", ["Shieldstone Mutual", "Shieldstone Insurance", "Shieldstone Mutual Group"]),
    ("Auberly Corporate", "Hospitality", ["Auberly Corporate", "Auberly Hotels Corp", "AUBERLY"]),
    ("Ironvale Electric", "Industrial", ["Ironvale Electric", "Ironvale", "Ironvale Electric Co."]),
    ("Pemberton Ames", "Financial Services", ["Pemberton Ames", "Pemberton Ames Financial", "PA Financial"]),
    ("Harborline Restaurants", "Hospitality", ["Harborline Restaurants", "Harborline", "Harborline Restaurants Inc"]),
    ("Crescent Bay Health", "Healthcare", ["Crescent Bay Health", "Crescent Bay Health South Florida", "Crescent Bay SF"]),
    ("Lakeshore Medicine", "Healthcare", ["Lakeshore Medicine", "Lakeshore Memorial", "LS Medicine"]),
    ("Wexford Markets", "Retail", ["Wexford Markets", "Wexford Super Markets", "Wexford Supermarkets Inc"]),
    ("Quantell Solutions", "Technology", ["Quantell Solutions", "Quantell", "Quantell Solutions Inc."]),
    ("Aerlight Airways", "Travel", ["Aerlight Airways", "Aerlight Airways Holdings", "ALW"]),
    ("Whitmore Rand", "Insurance", ["Whitmore Rand", "Whitmore & Rand", "Whitmore & Rand Companies"]),
]

AGENCIES = [
    "Meridian Event Partners", "BlueSky Meetings & Incentives", "Vantage Point Events",
    "Cornerstone Conference Group", "Luxe Gatherings International", "Summit Site Selection",
    "Apex Meeting Strategies", "Gilded Compass Events",
]

FIRST = ["Sarah", "Michael", "Jennifer", "David", "Amanda", "Robert", "Jessica", "James",
         "Emily", "Daniel", "Rachel", "Kevin", "Laura", "Brian", "Nicole", "Marcus",
         "Priya", "Carlos", "Elena", "Tom", "Grace", "Andre", "Megan", "Victor"]
LAST = ["Mitchell", "Chen", "Rodriguez", "Thompson", "Patel", "Williams", "Okafor",
        "Nakamura", "Fischer", "Delgado", "Kowalski", "Bennett", "Hargrove", "Osei",
        "Lindqvist", "Moreau", "Castillo", "Whitfield", "Iyer", "Brennan"]

EVENT_TYPES = ["Leadership Summit", "National Sales Kickoff", "Incentive Trip",
               "Product Launch", "Board Retreat", "Annual Gala", "Users Conference",
               "Training Program", "Holiday Party", "Awards Banquet"]

# Generic operational-note fragments (the unstructured haystack).
NOTE_FRAGMENTS = [
    "Client requested plated dinner service with wine pairings.",
    "Standard AV package: two projectors, confidence monitors, wireless lavs.",
    "Room block released 30 days out; light attrition.",
    "Buffet lunches all three days; boxed lunches for departure day.",
    "General session in the Grand Ballroom, 12 breakouts on mezzanine level.",
    "Late-night reception added on-site; billed to master.",
    "Client price-sensitive on F&B; negotiated per-attendee minimum.",
    "Rehearsal required day prior; ballroom hold from 2pm.",
    "Coffee service refreshed every 90 minutes per contract.",
    "Registration desk in the foyer with dedicated house phone.",
    "Client brought third-party production company for staging.",
    "Outdoor welcome reception moved indoors due to weather call at noon.",
    "VIP amenities delivered to 14 suites pre-arrival.",
    "Strict pharma compliance caps on per-person F&B spend documented.",
    "Union labor call for load-in at 6am; steward on site.",
]

# Signature fragments — each defines a gold-set retrieval query. Notes that
# contain the fragment are the ground-truth hits for the paired query.
SIGNATURE_FRAGMENTS = [
    {
        "key": "botanical_mocktail",
        "note": "Custom botanical and citrus mocktail reception was the highlight; "
                "mixology station with fresh herbs drew executive praise.",
        "query": "Looking to host a leadership summit with a heavy focus on "
                 "botanical citrus custom mocktail receptions and high-end AV.",
    },
    {
        "key": "vip_transport",
        "note": "Seamless VIP motorcade transport for C-suite arrivals; "
                "dedicated porte-cochere lane and advance security sweep.",
        "query": "We need seamless VIP transport and arrival logistics for about "
                 "150 executives attending a 3-day summit.",
    },
    {
        "key": "led_wall",
        "note": "Premium AV build with 40-foot LED wall, broadcast-grade cameras "
                "and live stream to remote offices.",
        "query": "Product launch requiring a large LED wall, broadcast cameras and "
                 "a live stream for remote attendees.",
    },
    {
        "key": "sustainability",
        "note": "Zero-waste catering mandate: compostable service ware, local farm "
                "sourcing documented for client ESG report.",
        "query": "Our company requires sustainable zero-waste catering with locally "
                 "sourced menus for an ESG-focused retreat.",
    },
    {
        "key": "kosher",
        "note": "Full glatt kosher program with rabbinical supervision; separate "
                "kitchen prep documented and certified.",
        "query": "Annual gala requiring a fully supervised kosher catering program.",
    },
    {
        "key": "wellness",
        "note": "Sunrise yoga on the event lawn and cold-press juice bar each "
                "morning; wellness track very well received.",
        "query": "Incentive trip with a wellness focus - morning yoga sessions and "
                 "healthy juice bar options for attendees.",
    },
]

SQL_SCHEMA = """
CREATE TABLE accounts (
    account_id   TEXT PRIMARY KEY,
    property_code TEXT NOT NULL,
    account_name TEXT NOT NULL,
    industry     TEXT,
    created_date TEXT,
    last_modified TEXT NOT NULL
);
CREATE TABLE agencies (
    agency_id   TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL,
    last_modified TEXT NOT NULL
);
CREATE TABLE contacts (
    contact_id    TEXT PRIMARY KEY,
    property_code TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    title         TEXT,
    account_id    TEXT REFERENCES accounts(account_id),
    agency_id     TEXT REFERENCES agencies(agency_id),
    last_modified TEXT NOT NULL
);
CREATE TABLE rfps (
    rfp_id        TEXT PRIMARY KEY,
    property_code TEXT NOT NULL,
    account_id    TEXT REFERENCES accounts(account_id),
    contact_id    TEXT REFERENCES contacts(contact_id),
    event_type    TEXT,
    attendee_count INTEGER,
    event_date    TEXT,
    status        TEXT,
    raw_text      TEXT,
    last_modified TEXT NOT NULL
);
CREATE TABLE beo_history (
    beo_id        TEXT PRIMARY KEY,
    property_code TEXT NOT NULL,
    account_id    TEXT REFERENCES accounts(account_id),
    contact_id    TEXT REFERENCES contacts(contact_id),
    rfp_id        TEXT REFERENCES rfps(rfp_id),
    event_type    TEXT,
    event_date    TEXT,
    attendee_count INTEGER,
    room_block    INTEGER,
    fb_spend      REAL,
    av_spend      REAL,
    total_revenue REAL,
    status        TEXT,
    ops_notes     TEXT,
    last_modified TEXT NOT NULL
);
"""


def _phone(rng: random.Random) -> str:
    styles = ["({}) {}-{}", "{}.{}.{}", "{}-{}-{}"]
    return rng.choice(styles).format(rng.randint(200, 989), rng.randint(200, 989), rng.randint(1000, 9999))


def generate(out_db: Path | None = None, goldset_path: Path | None = None) -> dict:
    rng = random.Random(SEED)
    out_db = out_db or settings.crm_db
    goldset_path = goldset_path or settings.goldset_path
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()

    con = sqlite3.connect(out_db)
    con.executescript(SQL_SCHEMA)

    for aid, name in [(f"AG{i:03d}", n) for i, n in enumerate(AGENCIES, 1)]:
        con.execute("INSERT INTO agencies VALUES (?,?,?)", (aid, name, T0))

    # Per-property account rows using the messy name variants.
    account_rows = []  # (account_id, property_code, canonical)
    seq = 0
    for canonical, industry, variants in ACCOUNTS:
        # Each account exists at 2-3 properties, under a different variant.
        props = rng.sample(PROPERTIES, k=rng.randint(2, 3))
        for (code, *_), variant in zip(props, rng.sample(variants, k=len(props))):
            seq += 1
            aid = f"{code}-ACC{seq:04d}"
            con.execute(
                "INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                (aid, code, variant, industry,
                 f"20{rng.randint(19, 25)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}", T0),
            )
            account_rows.append((aid, code, canonical))

    # Contacts — corporate planners tied to accounts, agency planners tied to
    # agencies. Some corporate contacts are duplicated across properties with
    # spelling drift but the same email (the contact-resolution challenge).
    contact_rows = []  # (contact_id, property_code, account_id|None, agency_id|None)
    cseq = 0
    # Same human, re-entered at another property's CRM: same email, name drift.
    by_canonical: dict[str, list[tuple[str, str, str]]] = {}
    for aid, code, canonical in account_rows:
        for _ in range(rng.randint(1, 2)):
            cseq += 1
            prior = by_canonical.get(canonical, [])
            if prior and rng.random() < 0.5:
                f, l, email = rng.choice(prior)
                name = rng.choice([f"{f[0]}. {l}", f"{f} {l}".upper(), f"{f} {l[0]}."])
            else:
                f, l = rng.choice(FIRST), rng.choice(LAST)
                name = f"{f} {l}"
                email = f"{f.lower()}.{l.lower()}@{canonical.lower().replace(' ', '').replace('&', 'and')[:12]}.com"
                by_canonical.setdefault(canonical, []).append((f, l, email))
            cid = f"{code}-CON{cseq:04d}"
            con.execute(
                "INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, code, name, email, _phone(rng),
                 rng.choice(["Corporate Travel Manager", "Executive Assistant", "Events Director", "Procurement Lead"]),
                 aid, None, T0),
            )
            contact_rows.append((cid, code, aid, None))

    agency_planner_ids = []
    for i, agency_name in enumerate(AGENCIES, 1):
        agid = f"AG{i:03d}"
        for _ in range(2):
            cseq += 1
            f, l = rng.choice(FIRST), rng.choice(LAST)
            code = rng.choice(PROPERTIES)[0]
            cid = f"{code}-CON{cseq:04d}"
            con.execute(
                "INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, code, f"{f} {l}",
                 f"{f.lower()}@{agency_name.split()[0].lower()}events.com",
                 _phone(rng), "Independent Event Planner", None, agid, T0),
            )
            agency_planner_ids.append((cid, code, agid))

    # BEO history — executed events with revenue and unstructured ops notes.
    beo_rows = []
    goldset: dict[str, dict] = {
        frag["key"]: {"query": frag["query"], "relevant_beo_ids": []} for frag in SIGNATURE_FRAGMENTS
    }
    bseq = 0
    for aid, code, canonical in account_rows:
        for _ in range(rng.randint(1, 4)):
            bseq += 1
            beo_id = f"{code}-BEO{bseq:05d}"
            attendees = rng.choice([40, 80, 120, 150, 200, 350, 500])
            fb = round(attendees * rng.uniform(95, 320), 2)
            av = round(rng.uniform(5_000, 140_000), 2)
            total = round(fb + av + attendees * rng.uniform(180, 420), 2)
            notes = rng.sample(NOTE_FRAGMENTS, k=3)
            # ~25% of BEOs get one signature fragment -> gold-set membership
            if rng.random() < 0.25:
                frag = rng.choice(SIGNATURE_FRAGMENTS)
                notes.insert(rng.randint(0, 2), frag["note"])
                goldset[frag["key"]]["relevant_beo_ids"].append(beo_id)
            planner = rng.choice([c for c in contact_rows if c[2] == aid] + agency_planner_ids)
            con.execute(
                "INSERT INTO beo_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (beo_id, code, aid, planner[0], None,
                 rng.choice(EVENT_TYPES),
                 f"202{rng.randint(3, 5)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
                 attendees, int(attendees * rng.uniform(0.4, 0.9)),
                 fb, av, total, "Executed", " ".join(notes), T0),
            )
            beo_rows.append(beo_id)

    # Open RFPs — inbound pipeline, some via agencies.
    rseq = 0
    for aid, code, canonical in rng.sample(account_rows, k=min(25, len(account_rows))):
        rseq += 1
        rfp_id = f"{code}-RFP{rseq:04d}"
        attendees = rng.choice([60, 100, 150, 250, 400])
        etype = rng.choice(EVENT_TYPES)
        planner = rng.choice([c for c in contact_rows if c[2] == aid] + agency_planner_ids)
        con.execute(
            "INSERT INTO rfps VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rfp_id, code, aid, planner[0], etype, attendees,
             f"2026-{rng.randint(8,12):02d}-{rng.randint(1,28):02d}",
             rng.choice(["Open", "Open", "Proposal Sent", "Negotiating"]),
             f"{etype} for approximately {attendees} attendees. "
             f"Requesting proposal including guest rooms, general session space, and F&B program.",
             T0),
        )

    con.commit()
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ["accounts", "agencies", "contacts", "rfps", "beo_history"]
    }
    con.close()

    goldset_path.write_text(json.dumps(goldset, indent=2))
    return counts


def mutate_delta(db_path: Path | None = None) -> dict:
    """Apply one simulated business day of CRM changes (all stamped DELTA_TS).

    Each change exercises a different incremental-sync code path:
      1. New source row for an EXISTING corporation under yet another legal-name
         variant -> incremental account ER must merge it into the canonical node.
      2. Brand-new account (Brightledger) -> new canonical node.
      3. New contact sharing an existing contact's email -> planner ER merge.
      4. New BEO for an existing account booked via an agency planner ->
         relationship upsert + REPRESENTS re-inference.
      5. New BEO + RFP for the new account -> full new-entity path.
      6. Status update on an existing RFP -> property update on an existing node.
    """
    db_path = db_path or settings.crm_db
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} not found - run `nexusvenue generate` first")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    if con.execute("SELECT 1 FROM accounts WHERE account_id = 'CHI-ACC9001'").fetchone():
        con.close()
        return {"status": "delta already applied"}

    # 1. Existing corporation, new property, new name variant.
    con.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                ("CHI-ACC9001", "CHI", "Ashcombe Incorporated", "Professional Services",
                 "2026-07-08", DELTA_TS))
    # 2. Brand-new account.
    con.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                ("MIA-ACC9002", "MIA", "Brightledger, Inc.", "Technology", "2026-07-08", DELTA_TS))

    # 3. Duplicate person: same email as an existing corporate contact, name drift.
    dup = con.execute(
        "SELECT * FROM contacts WHERE agency_id IS NULL AND email IS NOT NULL "
        "ORDER BY contact_id LIMIT 1").fetchone()
    first = dup["full_name"].split()[0].rstrip(".")
    drifted = f"{first[0]}. {dup['full_name'].split()[-1].rstrip('.')}"
    con.execute("INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?)",
                ("CHI-CON9001", "CHI", drifted, dup["email"], dup["phone"],
                 dup["title"], "CHI-ACC9001", None, DELTA_TS))
    # New contact at the new account.
    con.execute("INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?)",
                ("MIA-CON9002", "MIA", "Dana Whitfield", "dana.whitfield@brightledger.com",
                 "(415) 555-0184", "Events Director", "MIA-ACC9002", None, DELTA_TS))

    # 4. New BEO for an existing account, booked through an agency planner.
    existing_acct = con.execute(
        "SELECT account_id FROM accounts WHERE account_name LIKE 'Calder%' "
        "ORDER BY account_id LIMIT 1").fetchone()
    agency_planner = con.execute(
        "SELECT contact_id FROM contacts WHERE agency_id IS NOT NULL "
        "ORDER BY contact_id LIMIT 1").fetchone()
    con.execute("INSERT INTO beo_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ORL-BEO90001", "ORL", existing_acct["account_id"],
                 agency_planner["contact_id"], None, "Annual Gala", "2026-07-02",
                 400, 300, 92_400.0, 61_000.0, 213_800.0, "Executed",
                 "Rooftop drone light show finale synchronized to a live orchestra; "
                 "FAA waiver and spotter crew coordinated by hotel. "
                 "Client requested plated dinner service with wine pairings.",
                 DELTA_TS))
    # 5. New BEO + open RFP for the new account.
    con.execute("INSERT INTO beo_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("MIA-BEO90002", "MIA", "MIA-ACC9002", "MIA-CON9002", None,
                 "Users Conference", "2026-07-05", 250, 180, 58_750.0, 44_000.0,
                 148_500.0, "Executed",
                 "Developer conference with hack-lounge buildout; espresso cart "
                 "sponsorship and 24-hour grab-and-go. Standard AV package: two "
                 "projectors, confidence monitors, wireless lavs.",
                 DELTA_TS))
    con.execute("INSERT INTO rfps VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("MIA-RFP9001", "MIA", "MIA-ACC9002", "MIA-CON9002",
                 "Leadership Summit", 150, "2026-11-12", "Open",
                 "Leadership Summit for approximately 150 attendees. Requesting "
                 "proposal including guest rooms, general session space, and F&B program.",
                 DELTA_TS))

    # 6. Update an existing RFP's status.
    moved = con.execute(
        "SELECT rfp_id FROM rfps WHERE status = 'Open' ORDER BY rfp_id LIMIT 1").fetchone()
    con.execute("UPDATE rfps SET status = 'Proposal Sent', last_modified = ? WHERE rfp_id = ?",
                (DELTA_TS, moved["rfp_id"]))

    con.commit()
    con.close()
    return {
        "accounts": 2, "contacts": 2, "beo_history": 2, "rfps_new": 1,
        "rfps_updated": 1, "stamped": DELTA_TS,
    }


if __name__ == "__main__":
    print(generate())
