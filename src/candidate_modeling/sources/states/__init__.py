"""State connector registry. Each module exposes: STATE, SOURCE, ID_KEY, RESULT_ID, sync(), is_synced(),
search(name), load(con, profile).

To add a state: write `<state>.py` with that interface, register it here, add fixture tests.
"""
from . import arizona, georgia, michigan, north_carolina, pennsylvania, texas, wisconsin

REGISTRY = {m.STATE: m for m in (texas, michigan, pennsylvania, georgia, arizona, wisconsin, north_carolina)}

# Priority order for upcoming connectors (competitive / high-spend states first).
PLANNED = ["NV", "FL", "OH", "CA", "NY"]


def get(state: str):
    s = state.upper()
    if s not in REGISTRY:
        raise KeyError(f"No connector for {s} yet. Supported: {sorted(REGISTRY)}. Planned next: {PLANNED}")
    return REGISTRY[s]
