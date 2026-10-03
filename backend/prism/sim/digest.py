"""Order-sensitive digests of projected tables, for no-drift checks (M9 spec §5.1). A digest covers only the columns
it is given, so a column a projector adds later never changes the digest of the columns that were there before."""
import hashlib
import json
import sys
from collections.abc import Sequence

from prism.sim.model import TableData
from prism.sim.seed import PROJECTORS
from prism.sim.universe import SimConfig, build_universe


def table_digest(table: TableData, columns: Sequence[str]) -> str:
    idx = [table.columns.index(c) for c in columns]
    h = hashlib.sha256()
    for row in table.rows:
        h.update(repr(tuple(row[i] for i in idx)).encode())
        h.update(b"\n")
    return h.hexdigest()


def projection_digest(cfg: SimConfig) -> dict[str, dict]:
    u = build_universe(cfg)
    out = {}
    for db, project in PROJECTORS.items():
        for name, t in project(u).items():
            out[f"{db}.{name}"] = {"columns": list(t.columns), "rows": len(t.rows),
                                   "sha256": table_digest(t, t.columns)}
    return out


def main() -> int:
    json.dump({"small": projection_digest(SimConfig.small()), "full": projection_digest(SimConfig())},
              sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
