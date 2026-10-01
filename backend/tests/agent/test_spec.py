import pytest

from prism.agent.spec import (DashboardSpec, HandleInfo, fallback_spec, parse_spec, validate_spec)

H = {"r_1": HandleInfo("r_1", ["region", "ccy", "value"], "cashrecon", "open_breaks")}


def spec(**w):
    base = {"id": "w1", "type": "bar", "title": "Open breaks", "handle": "r_1",
            "encoding": {"x": "region", "y": "value"}}
    return DashboardSpec.model_validate({"widgets": [{**base, **w}], "narrative": "Looks fine."})


def test_valid_spec_has_no_problems():
    assert validate_spec(spec(), H) == []


@pytest.mark.parametrize("override,needle", [
    ({"handle": "r_other"}, "unknown handle"),
    ({"encoding": {"x": "nope", "y": "value"}}, "column"),
    ({"type": "kpi", "encoding": {}}, "value"),
    ({"type": "heatmap", "encoding": {"x": "region", "y": "ccy"}}, "value"),
])
def test_problems_are_reported(override, needle):
    assert any(needle in p for p in validate_spec(spec(**override), H))


def test_duplicate_widget_ids():
    s = DashboardSpec.model_validate({"widgets": [spec().widgets[0].model_dump()] * 2, "narrative": "x"})
    assert any("duplicate" in p for p in validate_spec(s, H))


def test_parse_spec_rejects_extra_fields_and_bad_types():
    with pytest.raises(ValueError):
        parse_spec({"widgets": [], "narrative": "x"})
    with pytest.raises(ValueError):
        parse_spec({"widgets": [{"id": "a", "type": "radar", "title": "t", "handle": "r_1", "encoding": {}}],
                    "narrative": "x"})
    with pytest.raises(ValueError):
        parse_spec({"widgets": [{"id": "a", "type": "bar", "title": "t", "handle": "r_1", "encoding": {},
                                 "rows": [[1]]}], "narrative": "x"})


def test_fallback_is_a_valid_table_on_the_last_handle():
    fb = fallback_spec(H, "r_1", "Here are the results.")
    assert fb.widgets[0].type == "table" and fb.widgets[0].handle == "r_1"
    assert validate_spec(fb, H) == []


def test_handle_info_defaults_for_sample_data():
    h = HandleInfo("r_1", ["a"], None, None)
    assert h.row_count == 0 and h.sample_rows == ()


def test_parse_spec_error_lists_field_paths_not_values():
    with pytest.raises(ValueError) as e:
        parse_spec({"widgets": [{"id": "a", "type": "bar", "title": "t", "handle": "r_1", "encoding": {},
                                 "rows": [["SECRET-VALUE"]]}], "narrative": "x"})
    assert "widgets.0.rows" in str(e.value) and "SECRET-VALUE" not in str(e.value)
