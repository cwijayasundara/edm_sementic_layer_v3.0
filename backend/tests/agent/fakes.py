from prism.agent.gateway_client import GatewayError


class FakeGateway:
    """responses: tool -> list of dicts/GatewayError/callables(args) consumed in order (the last one repeats)."""

    def __init__(self, responses: dict):
        self.responses = {k: list(v) if isinstance(v, list) else [v] for k, v in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, arguments))
        queue = self.responses.get(tool)
        assert queue, f"unexpected gateway call {tool}"
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        item = item(arguments) if callable(item) else item
        if isinstance(item, GatewayError):
            raise item
        return item


def summary(handle="r_aaaaaaaaaaaa", columns=("region", "value"), rows=((" EMEA", 3),), **kw):
    return {"handle": handle, "summary": {"handle": handle, "columns": list(columns), "column_count": len(columns),
            "columns_truncated": False, "row_count": len(rows), "sample_rows": [list(r) for r in rows],
            "units": kw.get("units", "breaks"), "truncated": False, "source": kw.get("source", "cashrecon"),
            **({"metric_id": kw["metric_id"]} if "metric_id" in kw else {})}}
