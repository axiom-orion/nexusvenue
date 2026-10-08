"""Deterministic entity-resolution evaluation against the generator's ground truth.

The mock-data generator knows which name variants belong to which company
(ACCOUNTS_BY_FLAVOR), so the account resolver can be scored without an LLM:
pairwise precision (of the pairs it merged, how many really are one company) and
recall (of the pairs that are one company, how many it merged). Precision is the
one that must not slip: a false merge puts two companies' spend on one node and
poisons every cross-property rollup.
"""

import copy
import random
from collections import defaultdict

from nexusvenue.etl.extract import extract
from nexusvenue.etl.resolve import resolve_accounts
from nexusvenue.mockdata.generate import ACCOUNTS_BY_FLAVOR


def ground_truth() -> dict[str, str]:
    """Account-name variant -> the company it really is (across every flavor)."""
    truth: dict[str, str] = {}
    for pool in ACCOUNTS_BY_FLAVOR.values():
        for canonical, _industry, variants in pool:
            for variant in variants:
                assert truth.setdefault(variant, canonical) == canonical, f"ambiguous variant {variant!r}"
    return truth


def degrade_contacts(raw: dict, fraction: float = 0.4, seed: int = 7) -> dict:
    """A copy of the CRM where `fraction` of corporate contacts use a free-mail
    address: a deterministic stand-in for sparse, noisy contact data."""
    rng = random.Random(seed)
    noisy = copy.deepcopy(raw)
    for c in noisy["contacts"]:
        if c.get("account_id") and rng.random() < fraction:
            c["email"] = f"{c['contact_id'].lower()}@gmail.com"
    return noisy


def evaluate_resolution(raw: dict | None = None, use_contacts: bool = True) -> dict:
    """Score the account resolver. use_contacts=False is the name-only baseline."""
    raw = raw or extract()
    truth = ground_truth()
    accounts = resolve_accounts(raw["accounts"], raw["contacts"] if use_contacts else None)
    cluster_of = {sid: a["canonical_id"] for a in accounts for sid in a["source_ids"]}
    rows = [r for r in raw["accounts"] if r["account_name"] in truth]  # delta rows have no ground truth

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
    misses = sorted(
        ({"canonical": c, "groups": sorted(sorted(set(n)) for n in g.values())}
         for c, g in by_truth.items() if len(g) > 1),
        key=lambda m: (-len(m["groups"]), m["canonical"]),
    )
    return {
        "accounts_source": len(raw["accounts"]),
        "accounts_canonical": len(accounts),
        "truth_companies": len(by_truth),
        "pairwise_precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "pairwise_recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "false_merges": fp,
        "missed_pairs": fn,
        "miss_count": len(misses),
        "misses": misses,
    }
