"""Fix CROSS canonical ids: split the 88 shared groups into singleton equivalence classes.

The semantic contract says members of one ``canonical_product_id`` must be the same
product (brand / category / material / model_spec equal). The ``CAN-000``..``CAN-087``
slice violates it: each id groups 5 unrelated products across 5 categories × 5 currencies.
This script gives each of those 440 products its own equivalence class, reusing the
``CAN-<product_id>`` convention already used by the demo fixture and seed tests.

Idempotent and safe: it aborts without writing unless exactly 440 records are changed,
and it only rewrites the ``canonical_product_id`` value (line kept byte-for-byte otherwise).

Usage:
    python3 scripts/fix_cross_canonical_ids.py [data/catalog-v1.jsonl]
"""

import re
import sys
from pathlib import Path

CROSS_RE = re.compile(r'"canonical_product_id": "CAN-\d{3}"')
PID_RE = re.compile(r'"product_id": "(P\d+)"')

EXPECTED = 440  # 88 groups × 5 members


def fix(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    out: list[str] = []
    for line in lines:
        if CROSS_RE.search(line):
            match = PID_RE.search(line)
            if match is None:
                raise ValueError(f"CROSS line without product_id: {line[:80]!r}")
            line = CROSS_RE.sub(
                f'"canonical_product_id": "CAN-{match.group(1)}"', line, count=1
            )
            changed += 1
        out.append(line)

    if changed != EXPECTED:
        raise ValueError(f"expected {EXPECTED} changes, got {changed}; nothing written")

    path.write_text("".join(out), encoding="utf-8")
    return changed


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/catalog-v1.jsonl")
    changed = fix(path)
    leftover = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if CROSS_RE.search(line))
    print(f"changed {changed} records; remaining CROSS ids: {leftover}")


if __name__ == "__main__":
    main()
