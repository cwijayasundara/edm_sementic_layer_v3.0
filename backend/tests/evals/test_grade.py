from prism.evals.cases import GoldenCase, Story, Tolerance
from prism.evals.grade import grade_golden, passed, rows_match, story_holds
from prism.evals.types import ChatResult, Table

REF = Table(["region", "value"], [["EMEA", 12], ["APAC", 3], ["AMER", "5.0"]])


def widget(handle="r_aaaaaaaaaaaa", type_="bar", metric_id="open_breaks", source="cashrecon"):
    return {"type": "widget", "widget": {"id": "w1", "type": type_, "title": "T", "handle": handle, "encoding": {}},
            "handle_info": {"metric_id": metric_id, "source": source}}


def case(**expect):
    return GoldenCase.model_validate({"id": "c_1", "persona": "head_data", "question": "q?",
                                      "reference": {"tool": "run_metric", "args": {"metric_id": "open_breaks"}},
                                      "expect": {"metric_id": "open_breaks", **expect}})


def test_rows_match_ignores_order_and_column_order_and_parses_numeric_strings():
    got = Table(["value", "region"], [[5, "AMER"], [12.0000001, "EMEA"], [3, "APAC"]])
    assert rows_match(REF, got, Tolerance(rel=1e-6)).ok


def test_rows_match_reports_value_column_and_row_differences():
    assert not rows_match(REF, Table(["region", "value"], [["EMEA", 12], ["APAC", 4], ["AMER", 5]]), Tolerance()).ok
    assert not rows_match(REF, Table(["region", "n"], REF.rows), Tolerance()).ok
    assert not rows_match(REF, Table(["region", "value"], REF.rows[:2]), Tolerance()).ok
    assert rows_match(REF, Table(["region", "value"], [["EMEA", 12.5], ["APAC", 3], ["AMER", 5]]),
                      Tolerance(abs=0.5)).ok


def test_story_kinds():
    t = Table(["vendor_id", "asset_class", "value"], [["V_A", "Corp bond", 40], ["V_B", "Corp bond", 30],
                                                      ["V_B", "Equity", 90]])
    top = Story.model_validate({"top": {"by": "value", "key": "vendor_id", "equals": "V_A",
                                        "where": {"asset_class": "Corp bond"}}})
    assert story_holds(top, t)
    assert not story_holds(Story.model_validate({"top": {"by": "value", "key": "vendor_id", "equals": "V_A"}}), t)
    assert story_holds(Story.model_validate({"contains": {"vendor_id": "V_B", "asset_class": "Equity"}}), t)
    assert not story_holds(Story.model_validate({"contains": {"vendor_id": "V_C"}}), t)
    assert story_holds(Story.model_validate({"count": {"min": 3, "max": 3}}), t)
    assert not story_holds(Story.model_validate({"top": {"by": "missing", "key": "vendor_id", "equals": "V_A"}}), t)


def test_grade_golden_passes_a_correct_answer():
    chat = ChatResult(widgets=[widget()], summary="ok")
    checks = grade_golden(case(chart_types=["bar"]), chat, {"r_aaaaaaaaaaaa": REF}, REF)
    assert passed(checks) and [c.name for c in checks] == ["answered", "routing", "rows", "chart"]


def test_rows_are_reported_not_required_when_the_case_has_a_story():
    other = Table(["region", "value"], [["EMEA", 12]])         # a different grouping that still tells the story
    chat = ChatResult(widgets=[widget()])
    checks = grade_golden(case(story=[{"top": {"by": "value", "key": "region", "equals": "EMEA"}}]), chat,
                          {"r_aaaaaaaaaaaa": other}, REF)
    rows = next(c for c in checks if c.name == "rows")
    assert not rows.ok and not rows.required and passed(checks)


def test_grade_golden_failures():
    assert not passed(grade_golden(case(), ChatResult(error={"code": "run_limit"}), {}, REF))
    wrong = ChatResult(widgets=[widget(metric_id="aged_open_breaks")])
    assert not passed(grade_golden(case(), wrong, {"r_aaaaaaaaaaaa": REF}, REF))
    expired = ChatResult(widgets=[widget()])
    checks = grade_golden(case(), expired, {"r_aaaaaaaaaaaa": None}, REF)
    assert not passed(checks)
    no_ref = grade_golden(case(), ChatResult(widgets=[widget()]), {"r_aaaaaaaaaaaa": REF}, None)
    assert next(c for c in no_ref if c.name == "rows").detail == "the reference could not be computed"
