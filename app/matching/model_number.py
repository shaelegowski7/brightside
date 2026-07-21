"""Model-number regex extraction from a deal title — spec priority #2:
"Model number regex from deal title (patterns like AF300UK, alphanumeric
model codes)". HUKD titles are often noisy multi-item posts ("Forge Steel
Sale | E.g. 2 x 8m Tape Measure / Drive Socket 3/8" Set 18 Pcs - £17.49 /
..."), so this is deliberately conservative: only letter-prefixed
alphanumeric tokens count (AF300UK, DCS355N-XJ). That excludes voltage/
wattage/size specs (18V, 50W, 4.7L, 3/8") and prices (£17.49), which are
digit-first and would otherwise feed garbage into the Keepa search. This is
a heuristic, not a guarantee — a wrong match is worse than no match, which
is why every result from this path carries confidence='low' (see
pipeline.py) rather than being treated as a verified match."""
import re

_CANDIDATE_RE = re.compile(r"\b[A-Za-z]{1,4}-?\d{2,}[A-Za-z0-9-]{0,10}\b")
_MIN_LENGTH = 5


def extract_model_number(title: str) -> str | None:
    candidates = [m.group(0) for m in _CANDIDATE_RE.finditer(title) if len(m.group(0)) >= _MIN_LENGTH]
    if not candidates:
        return None
    return max(candidates, key=len).upper()
