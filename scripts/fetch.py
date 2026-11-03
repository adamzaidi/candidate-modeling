"""Usage: python scripts/fetch.py <slug> [--lookup]"""
import argparse

from candidate_modeling.config import load_profile, raw_dir, key
from candidate_modeling.sources import fec, openstates

SOURCES = {"fec": fec, "openstates": openstates}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--lookup", action="store_true", help="search sources for IDs to put in the profile")
    args = ap.parse_args()
    p = load_profile(args.slug)

    if args.lookup:
        print("FEC matches:")
        for c in fec.search_candidate(p["name"].split()[-1], p.get("state")):
            print("  ", c)
        if key("OPENSTATES_API_KEY"):
            print("Open States matches:")
            for c in openstates.search_person(p["name"]):
                print("  ", c)
        return

    for name, mod in SOURCES.items():
        try:
            mod.fetch(p, raw_dir(args.slug, name))
        except Exception as e:
            print(f"  {name}: failed — {e}")


if __name__ == "__main__":
    main()
