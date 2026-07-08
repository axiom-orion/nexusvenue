"""Generate a realistically messy multi-property hospitality CRM in SQLite.

Simulates three property-level Salesforce orgs (Orlando, Miami, Chicago) that
were never integrated: the same corporate account appears under different
legal-name variants per property, contacts are duplicated with inconsistent
spellings, and unstructured BEO operational notes carry the institutional
knowledge that never made it into structured fields.

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

PROPERTIES = [
    ("ORL", "NexusVenue Grand Orlando", "Orlando, FL", 1200),
    ("MIA", "NexusVenue Miami Beachfront", "Miami, FL", 800),
    ("CHI", "NexusVenue Chicago Riverside", "Chicago, IL", 950),
]

# Canonical corporate accounts with per-property name variants (the entity
# resolution challenge). (canonical, industry, [variants])
ACCOUNTS = [
    ("Deloitte", "Professional Services", ["Deloitte", "Deloitte LLP", "DELOITTE & TOUCHE LLP"]),
    ("Salesforce", "Technology", ["Salesforce", "Salesforce.com, Inc.", "SFDC"]),
    ("Pfizer", "Pharmaceutical", ["Pfizer", "Pfizer Inc.", "Pfizer Incorporated"]),
    ("JPMorgan Chase", "Financial Services", ["JPMorgan Chase", "J.P. Morgan", "JPMorgan Chase & Co"]),
    ("Lockheed Martin", "Aerospace & Defense", ["Lockheed Martin", "Lockheed Martin Corp.", "LOCKHEED-MARTIN"]),
    ("Procter & Gamble", "Consumer Goods", ["Procter & Gamble", "P&G", "Procter and Gamble Co"]),
    ("Accenture", "Professional Services", ["Accenture", "Accenture PLC", "Accenture Federal Services"]),
    ("Cisco", "Technology", ["Cisco", "Cisco Systems", "Cisco Systems, Inc."]),
    ("AbbVie", "Pharmaceutical", ["AbbVie", "AbbVie Inc.", "Abbvie"]),
    ("State Farm", "Insurance", ["State Farm", "State Farm Insurance", "State Farm Mutual"]),
    ("Hyatt Corporate", "Hospitality", ["Hyatt Corporate", "Hyatt Hotels Corp", "HYATT"]),
    ("Emerson Electric", "Industrial", ["Emerson Electric", "Emerson", "Emerson Electric Co."]),
    ("Raymond James", "Financial Services", ["Raymond James", "Raymond James Financial", "RJ Financial"]),
    ("Darden Restaurants", "Hospitality", ["Darden Restaurants", "Darden", "Darden Restaurants Inc"]),
    ("Baptist Health", "Healthcare", ["Baptist Health", "Baptist Health South Florida", "Baptist Health SF"]),
    ("Northwestern Medicine", "Healthcare", ["Northwestern Medicine", "Northwestern Memorial", "NW Medicine"]),
    ("Publix", "Retail", ["Publix", "Publix Super Markets", "Publix Supermarkets Inc"]),
    ("Motorola Solutions", "Technology", ["Motorola Solutions", "Motorola", "Motorola Solutions Inc."]),
    ("United Airlines", "Travel", ["United Airlines", "United Airlines Holdings", "UAL"]),
    ("Marsh McLennan", "Insurance", ["Marsh McLennan", "Marsh & McLennan", "Marsh & McLennan Companies"]),
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
    created_date TEXT
);
CREATE TABLE agencies (
    agency_id   TEXT PRIMARY KEY,
    agency_name TEXT NOT NULL
);
CREATE TABLE contacts (
    contact_id    TEXT PRIMARY KEY,
    property_code TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    title         TEXT,
    account_id    TEXT REFERENCES accounts(account_id),
    agency_id     TEXT REFERENCES agencies(agency_id)
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
    raw_text      TEXT
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
    ops_notes     TEXT
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
        con.execute("INSERT INTO agencies VALUES (?,?)", (aid, name))

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
                "INSERT INTO accounts VALUES (?,?,?,?,?)",
                (aid, code, variant, industry, f"20{rng.randint(19, 25)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}"),
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
                "INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?)",
                (cid, code, name, email, _phone(rng),
                 rng.choice(["Corporate Travel Manager", "Executive Assistant", "Events Director", "Procurement Lead"]),
                 aid, None),
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
                "INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?)",
                (cid, code, f"{f} {l}",
                 f"{f.lower()}@{agency_name.split()[0].lower()}events.com",
                 _phone(rng), "Independent Event Planner", None, agid),
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
                "INSERT INTO beo_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (beo_id, code, aid, planner[0], None,
                 rng.choice(EVENT_TYPES),
                 f"202{rng.randint(3, 5)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
                 attendees, int(attendees * rng.uniform(0.4, 0.9)),
                 fb, av, total, "Executed", " ".join(notes)),
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
            "INSERT INTO rfps VALUES (?,?,?,?,?,?,?,?,?)",
            (rfp_id, code, aid, planner[0], etype, attendees,
             f"2026-{rng.randint(8,12):02d}-{rng.randint(1,28):02d}",
             rng.choice(["Open", "Open", "Proposal Sent", "Negotiating"]),
             f"{etype} for approximately {attendees} attendees. "
             f"Requesting proposal including guest rooms, general session space, and F&B program."),
        )

    con.commit()
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ["accounts", "agencies", "contacts", "rfps", "beo_history"]
    }
    con.close()

    goldset_path.write_text(json.dumps(goldset, indent=2))
    return counts


if __name__ == "__main__":
    print(generate())
