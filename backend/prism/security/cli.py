"""prism-token: print a bearer token for a demo persona (manual API testing)."""
import argparse
import sys

from prism.config import Settings
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import mint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prism-token")
    parser.add_argument("persona", choices=sorted(PERSONAS))
    parser.add_argument("audience", help="e.g. refmaster-api or marketmaster-api")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args(argv)
    print(mint(claims_for(args.persona), args.audience, Settings().jwt_secret.get_secret_value(), ttl_s=args.ttl))
    return 0


if __name__ == "__main__":
    sys.exit(main())
