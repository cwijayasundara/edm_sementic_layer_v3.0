"""JOINABLE_ON (M9 spec §3.2): the semantic layer's statement of how the five systems connect."""
import pytest

from prism.graph.model import build_graph


@pytest.fixture(scope="module")
def links(graph_embedder) -> set[frozenset]:
    g = build_graph(graph_embedder)
    out = set()
    for a, typ, b, props in g.edges:
        if typ == "JOINABLE_ON":
            ma, mb = a.removeprefix("metric:"), b.removeprefix("metric:")
            assert ma < mb and g.nodes[a]["props"]["source"] != g.nodes[b]["props"]["source"]
            assert set(props) == {"key", "other_key"}
            out.add(frozenset({(ma, props["key"]), (mb, props["other_key"])}))
    return out


@pytest.mark.parametrize("hop", [
    (("late_feeds", "source_id"), ("funds", "custodian_source_id")),                         # FeedHub -> AssetRecon
    (("pending_corporate_actions", "security_id"), ("position_exceptions", "security_id")),  # RefMaster -> AssetRecon
    (("open_dq_exceptions", "record_ref"), ("position_exceptions", "security_id")),
    (("price_suspects", "security_id"), ("position_exceptions", "security_id")),             # MarketMaster -> AssetRecon
    (("price_suspects", "security_id"), ("pending_corporate_actions", "security_id")),       # MarketMaster -> RefMaster
    (("funds", "fund_entity_id"), ("open_breaks", "legal_entity_id")),                       # AssetRecon -> CashRecon
    (("funds", "fund_entity_id"), ("pending_corporate_actions", "issuer_entity_id")),
    (("open_breaks", "bank_source_id"), ("late_feeds", "source_id")),                        # CashRecon -> FeedHub
])
def test_every_hop_of_the_chain_is_joinable(links, hop):
    assert frozenset(hop) in links


def test_a_custodian_is_never_joined_to_a_bank(links):
    assert frozenset({("funds", "custodian_source_id"), ("open_breaks", "bank_source_id")}) not in links


def test_same_source_metrics_are_never_linked(links):
    assert frozenset({("funds", "portfolio_id"), ("position_exceptions", "portfolio_id")}) not in links
