"""prism-seed: create and populate the simulated platform databases."""
import argparse
import sys
import time

from prism.config import APP_DB, LOGICAL_DBS, ConfigError, load_settings
from prism.sim.seed import is_seeded, seed_all
from prism.sim.universe import SimConfig

CHECK_SEEDED, CHECK_NOT_SEEDED, CHECK_ERROR = 0, 1, 2


def check() -> int:
    """Exit code for --check: 0 seeded, 1 definitely not seeded, 2 on ANY error (never a reason to re-seed)."""
    try:
        return CHECK_SEEDED if is_seeded(load_settings()) else CHECK_NOT_SEEDED
    except ConfigError as exc:
        print(f"prism-seed --check failed: {exc}", file=sys.stderr)
        return CHECK_ERROR
    except Exception as exc:  # noqa: BLE001 - any failure is "unknown", which callers must not treat as "not seeded"
        # type only: a driver message can quote connection parameters
        print(f"prism-seed --check failed: {type(exc).__name__} (is Postgres up? run `make db`)", file=sys.stderr)
        return CHECK_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prism-seed", description="Create and seed the simulated platforms.")
    parser.add_argument("--reset", action="store_true", help="drop and re-seed even if already seeded")
    parser.add_argument("--check", action="store_true",
                        help="exit 0 if seeded with the current key, 1 if not seeded, 2 on any error")
    parser.add_argument("--small", action="store_true", help="small dataset for quick demos")
    parser.add_argument("--scale", type=float, default=1.0, help="multiply securities, entities, portfolios and cash accounts (default 1)")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"prism-seed: {exc}", file=sys.stderr)
        return 2
    if not args.reset and is_seeded(settings):
        print("Already seeded (use --reset to re-seed).")
        return 0
    base = dict(seed=settings.seed, as_of=settings.as_of)
    cfg = SimConfig.small(**base, scale=args.scale) if args.small else SimConfig(**base, scale=args.scale)
    doomed = [settings.dbname(logical) for logical in LOGICAL_DBS]
    print(f"Dropping and recreating databases on {settings.pg_host}:{settings.pg_port}: {', '.join(doomed)} "
          f"(keeping {settings.dbname(APP_DB)})")
    started = time.monotonic()
    counts = seed_all(settings, cfg)
    for db, tables in counts.items():
        print(f"{db}: " + ", ".join(f"{name}={n}" for name, n in tables.items()))
    print(f"Seeded ({cfg.profile}) in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
