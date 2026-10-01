"""Container for one generated table, plus a sequential id helper."""
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TableData:
    columns: tuple[str, ...]
    rows: list[tuple] = field(default_factory=list)

    def add(self, *values) -> None:
        if len(values) != len(self.columns):
            raise ValueError(f"expected {len(self.columns)} values for {self.columns}, got {len(values)}")
        self.rows.append(values)

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def id_sequence() -> Callable[..., str]:
    counters: dict[str, int] = defaultdict(int)

    def nxt(prefix: str, width: int = 7) -> str:
        counters[prefix] += 1
        return f"{prefix}{counters[prefix]:0{width}d}"

    return nxt
