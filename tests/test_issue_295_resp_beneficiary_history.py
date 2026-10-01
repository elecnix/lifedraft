"""Issue #295: RESP grants start each beneficiary from their DECLARED history.

Before #295 the contract recorded one set of grant totals per RESP account,
the adapter summed them and the opening state split them evenly across every
child, and the grant calculator started every child at zero history: the
$7,200 CESG / $3,600 QESI lifetime caps restarted at $0, the 16-17
contribution test ignored contributions made before the projection, unused
grant room (catch-up) was never used, and the $50,000 lifetime contribution
limit was never checked in the fold.

Every behaviour test here enters through a contract document
(``input_contract.to_internal_config``), builds ``SimulationConfig`` and runs
``FamilySimulation.run()`` -- in both the yearly and the monthly time step
where the fold differs -- and asserts on the engine's own per-year output
(``YearResult.resp_cesg_paid`` / ``resp_qesi_paid`` /
``resp_contributions_paid`` / ``resp_lifetime_contributions`` /
``resp_contribution_redirected`` and ``contributions``), never on state a test
built by hand (DP#11). All figures are fabricated round numbers; people are
the shipped example's role-based ids (p1, p2, ca, cb) -- DP#4/DP#15.
"""
import copy
import os
import subprocess
import sys

import pytest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)
sys.path.insert(0, _TESTS)
sys.path.insert(0, _ROOT)

import contract_schema  # noqa: E402
import input_contract as ic  # noqa: E402
import model_fidelity  # noqa: E402
import test_input_contract as tic  # noqa: E402
from contract_errors import ContractAdaptationError, ContractValidationError  # noqa: E402
from countries.canada.adapter import CanadaAdapter  # noqa: E402
from countries.canada.resp_rules import RESPCalculator  # noqa: E402
from scenario_overlay import ScenarioOverlay, apply_overlay  # noqa: E402
from simulation import FamilySimulation  # noqa: E402
from simulation_config import SimulationConfig  # noqa: E402
from simulation_state import initial_state_for_run  # noqa: E402
from strategy import AllocationStrategy  # noqa: E402
from trajectory_invariants import assert_run_invariants  # noqa: E402

START_YEAR = 2026  # the shipped example's as_of year
MODES = ('yearly', 'monthly')


# ── Contract fixtures ────────────────────────────────────────────────────────

def _beneficiary(person, *, contributions=0.0, before_15=None, basic=0.0,
                 additional=0.0, qesi=0.0, clb=0.0, years=0):
    """One accounts[].resp.beneficiaries[] entry. ``before_15`` defaults to
    the whole contribution total (a child still under 15 at as_of)."""
    return {
        "person": person,
        "contributions_total": contributions,
        "contributions_before_age_15": contributions if before_15 is None else before_15,
        "cesg_basic_received": basic,
        "cesg_additional_received": additional,
        "qesi_received": qesi,
        "clb_received": clb,
        "years_with_100_before_age_15": years,
    }


def _principal(b):
    return (b["contributions_total"] + b["cesg_basic_received"]
            + b["cesg_additional_received"] + b["qesi_received"] + b["clb_received"])


def _doc(children, beneficiaries, *, balance=None, province="quebec"):
    """The shipped example trimmed to p1/p2 plus ``children`` (a dict
    ``{id: birth_date}`` over the example's ca/cb), with ONE family RESP whose
    beneficiaries are ``beneficiaries`` and whose account totals are their
    sums. ``balance`` defaults to the beneficiaries' principal (no earnings)."""
    doc = tic._two_generation_subset(tic._load_example())
    keep = {"p1", "p2"} | set(children)
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
        if p["id"] in children:
            p["birth_date"] = children[p["id"]]
            p["residency"] = {"province": province, "since": children[p["id"]]}
            # No declared study period: the RESP winds down on the age-derived
            # window, so a child's balance is not drained in the start year.
            p["study_periods"] = []
    doc["jurisdiction"]["province"] = province
    resp_acc = next(a for a in doc["accounts"] if a["kind"] == "resp")
    resp = resp_acc["resp"]
    resp["beneficiaries"] = beneficiaries
    resp["contributions_total"] = sum(b["contributions_total"] for b in beneficiaries)
    resp["cesg_received"] = sum(b["cesg_basic_received"] + b["cesg_additional_received"]
                                for b in beneficiaries)
    resp["qesi_received"] = sum(b["qesi_received"] for b in beneficiaries)
    resp["clb_received"] = sum(b["clb_received"] for b in beneficiaries)
    resp_acc["balance"]["amount"] = (sum(_principal(b) for b in beneficiaries)
                                     if balance is None else balance)
    return doc


def _strategy(resp_pct):
    return AllocationStrategy(name=f"resp {resp_pct}", rrsp_pct=0.2, spousal_rrsp_pct=0.0,
                              tfsa_pct=0.1, resp_pct=resp_pct,
                              non_reg_pct=max(0.0, 0.7 - resp_pct))


def _config(doc, mode="yearly"):
    cfg = ic.to_internal_config(doc)
    cfg["assumptions"]["time_step"] = mode
    return SimulationConfig.from_dict(cfg)


def _sim(doc, mode="yearly", resp_pct=0.5):
    config = _config(doc, mode)
    return FamilySimulation(config, adapter=CanadaAdapter(config),
                            strategy=_strategy(resp_pct))


def _run(doc, mode="yearly", resp_pct=0.5):
    sim = _sim(doc, mode, resp_pct)
    results = sim.run()
    # The engine already asserts these inside run(); asserted again here so a
    # regression that unwires them from run() cannot hide a #295 breach.
    assert_run_invariants(results, sim.config)
    return results


# ── Issue test 1: a child at the $7,200 lifetime CESG maximum ───────────────

@pytest.mark.parametrize("mode", MODES)
def test_child_at_lifetime_cesg_max_gets_zero_cesg_every_year(mode):
    """Born 2012 (age 14): $7,500 of basic room accrued through 2026, and a
    declared $7,000 basic + $200 additional = the $7,200 lifetime maximum.
    Contributions keep flowing while the child is under 18, and not one
    dollar of CESG is paid in any projected year."""
    doc = _doc({"ca": "2012-05-01"},
               [_beneficiary("ca", contributions=36000, basic=7000, additional=200)])
    results = _run(doc, mode)
    under_18 = [r for r in results if START_YEAR + r.year - 1 - 2012 <= 17]
    assert any(r.contributions["resp"] > 0 for r in under_18), \
        "vacuous: no RESP contribution while the child could earn CESG"
    assert all(r.resp_cesg_paid[0] == 0 for r in results)

    # The same child with NO grants received does earn CESG in year 0: the
    # zero above comes from the seeded lifetime counter, nothing else.
    fresh = _doc({"ca": "2012-05-01"}, [_beneficiary("ca", contributions=36000)])
    assert _run(fresh, mode)[0].resp_cesg_paid[0] > 0


# ── Issue test 2: the 16-17 test is decided by the declared history ─────────

def _sixteen_year_old(before_15, years=0):
    # Born 2010: 16 in 2026, 17 in 2027.
    return _doc({"ca": "2010-05-01"},
                [_beneficiary("ca", contributions=max(before_15, 100.0),
                              before_15=before_15, basic=0.0, years=years)])


@pytest.mark.parametrize("mode", MODES)
def test_16_year_old_meeting_pre_15_test_gets_cesg_in_year_0(mode):
    results = _run(_sixteen_year_old(2000.0), mode)
    assert results[0].resp_cesg_paid[0] > 0


@pytest.mark.parametrize("mode", MODES)
def test_16_year_old_not_meeting_pre_15_test_gets_none(mode):
    results = _run(_sixteen_year_old(0.0), mode)
    assert results[0].contributions["resp"] > 0, "vacuous: nothing contributed"
    assert results[0].resp_cesg_paid[0] == 0
    assert results[1].resp_cesg_paid[0] == 0  # age 17


def test_pre_15_dollar_prong_boundary():
    """DP#17: $1,999.99 before 15 (no $100 years) fails, $2,000.00 passes."""
    assert _run(_sixteen_year_old(1999.99))[0].resp_cesg_paid[0] == 0
    assert _run(_sixteen_year_old(2000.00))[0].resp_cesg_paid[0] > 0


def test_pre_15_years_prong_boundary():
    """DP#17: four declared years with $100 ($400) pass, three ($300) fail."""
    assert _run(_sixteen_year_old(400.0, years=4))[0].resp_cesg_paid[0] > 0
    assert _run(_sixteen_year_old(300.0, years=3))[0].resp_cesg_paid[0] == 0


def test_fourth_qualifying_year_can_be_the_start_year():
    """Turns 15 in 2026 with 3 declared years: the year-0 contribution (a
    projection year, never counted in the declaration) is the fourth, so the
    child still gets CESG at 16."""
    doc = _doc({"ca": "2011-05-01"},
               [_beneficiary("ca", contributions=300.0, before_15=300.0, years=3)])
    results = _run(doc)
    assert results[0].resp_contributions_paid[0] >= 100
    assert results[1].resp_cesg_paid[0] > 0


# ── Issue test 3: unused grant room reaches the fold ────────────────────────

def _ten_year_old(basic):
    # Born 2016: $5,000 of basic room accrued through 2025 (10 x $500).
    return _doc({"ca": "2016-05-01"},
                [_beneficiary("ca", contributions=10000.0, basic=basic)])


def test_unused_room_pays_1000_basic_cesg_on_5000_contribution():
    """$2,000 of basic CESG received leaves $3,000 of room carried forward.
    The allocator funds the grant-maximising $5,000 and the child receives
    $1,000 of basic CESG (the household earns $214,000: no additional CESG)."""
    results = _run(_ten_year_old(2000.0))
    assert results[0].contributions["resp"] == 5000
    assert results[0].resp_contributions_paid[0] == 5000
    assert results[0].resp_cesg_paid[0] == 1000


def test_no_unused_room_pays_500_basic_cesg():
    """Basic received equal to the room accrued through 2025: nothing to
    catch up, the allocator funds $2,500 and the child receives $500."""
    results = _run(_ten_year_old(5000.0))
    assert results[0].contributions["resp"] == 2500
    assert results[0].resp_cesg_paid[0] == 500


def test_unused_room_catch_up_in_monthly_mode():
    """The monthly fold uses the same grant path: $1,000 with room, $500
    without (monthly allocations may exceed the grant-maximising amount; the
    basic grant is still capped at twice the annual room)."""
    assert _run(_ten_year_old(2000.0), "monthly")[0].resp_cesg_paid[0] == 1000
    assert _run(_ten_year_old(5000.0), "monthly")[0].resp_cesg_paid[0] == 500


# ── Issue test 4: a family plan keeps per-child figures unaveraged ──────────

def _family_plan():
    # child_a (ca) born 2007: 19 in 2026, past the grant window.
    # child_b (cb) born 2018: 8 in 2026, still eligible.
    ben_a = _beneficiary("ca", contributions=30000.0, before_15=24000.0, basic=6000.0,
                         additional=400.0, qesi=3000.0, clb=0.0, years=12)
    ben_b = _beneficiary("cb", contributions=6000.0, basic=1200.0, additional=0.0,
                         qesi=600.0, clb=500.0, years=5)
    return _doc({"ca": "2007-05-01", "cb": "2018-05-01"}, [ben_a, ben_b],
                balance=60000.0), ben_a, ben_b


def test_family_plan_per_child_grants_survive_mapping_unaveraged():
    doc, ben_a, ben_b = _family_plan()
    cfg = ic.to_internal_config(doc)
    children = {c["id"]: c for c in cfg["family"]["children"]}
    for pid, ben in (("ca", ben_a), ("cb", ben_b)):
        history = children[pid]["resp_history"]
        for key in ("contributions_total", "contributions_before_age_15",
                    "cesg_basic_received", "cesg_additional_received",
                    "qesi_received", "clb_received", "years_with_100_before_age_15"):
            assert history[key] == ben[key], (pid, key)
        assert history["family_plan"] is True

    config = SimulationConfig.from_dict(cfg)
    canada = initial_state_for_run(config).jurisdiction_state["canada"]
    order = [c["id"] for c in config.children]
    ia, ib = order.index("ca"), order.index("cb")
    assert canada["resp_contributions"][ia] == 30000.0
    assert canada["resp_contributions"][ib] == 6000.0
    assert canada["resp_cesg"][ia] == 6400.0           # basic + additional
    assert canada["resp_cesg"][ib] == 1700.0           # basic + CLB (a grant)
    assert canada["resp_qesi"][ia] == 3000.0
    assert canada["resp_qesi"][ib] == 600.0
    assert sum(canada["resp_contributions"]) == 36000.0
    assert sum(canada["resp_cesg"]) == cfg["accounts"]["resp_composition"]["total_cesg_received"]
    assert abs(sum(canada["resp_balances"]) - config.resp_current_balance) <= 0.01
    # Balance attributed pro rata to principal (39,400 : 8,300), not halved.
    assert canada["resp_balances"][ia] == pytest.approx(60000.0 * 39400 / 47700)


@pytest.mark.parametrize("mode", MODES)
def test_family_plan_only_the_eligible_child_earns_grants(mode):
    doc, _a, _b = _family_plan()
    config = _config(doc, mode)
    order = [c["id"] for c in config.children]
    ia, ib = order.index("ca"), order.index("cb")
    results = _run(doc, mode)
    assert results[0].resp_cesg_paid[ia] == 0
    assert results[0].resp_cesg_paid[ib] > 0


# ── Issue test 5: account totals must equal the sum over beneficiaries ──────

def _two_child_doc():
    return _doc({"ca": "2014-05-01", "cb": "2018-05-01"},
                [_beneficiary("ca", contributions=10000.0, basic=2000.0, qesi=1000.0, clb=500.0),
                 _beneficiary("cb", contributions=4000.0, basic=800.0, additional=100.0,
                              qesi=400.0)])


@pytest.mark.parametrize("field,delta", [
    ("contributions_total", 1.0), ("cesg_received", 1.0),
    ("qesi_received", -1.0), ("clb_received", 1.0),
    ("contributions_total", 0.02),
])
def test_account_totals_disagreeing_with_beneficiaries_refused_at_load(field, delta):
    doc = _two_child_doc()
    resp = next(a for a in doc["accounts"] if a["kind"] == "resp")["resp"]
    resp[field] += delta
    with pytest.raises(ContractAdaptationError) as err:
        ic.to_internal_config(doc)
    assert field in str(err.value) and "family_resp" in str(err.value)


def test_account_total_within_one_cent_is_accepted():
    doc = _two_child_doc()
    resp = next(a for a in doc["accounts"] if a["kind"] == "resp")["resp"]
    resp["contributions_total"] += 0.01
    ic.to_internal_config(doc)  # no refusal


def _refused(doc):
    with pytest.raises(ContractAdaptationError):
        ic.to_internal_config(doc)


def test_duplicate_beneficiary_refused():
    ben = _beneficiary("ca", contributions=1000.0)
    _refused(_doc({"ca": "2014-05-01"}, [ben, copy.deepcopy(ben)]))


def test_beneficiary_who_is_not_a_child_refused():
    _refused(_doc({"ca": "2014-05-01"}, [_beneficiary("p1", contributions=1000.0)]))


def test_beneficiary_without_birth_date_refused():
    doc = _doc({"ca": "2014-05-01"}, [_beneficiary("ca", contributions=1000.0)])
    next(p for p in doc["people"] if p["id"] == "ca")["birth_date"] = None
    _refused(doc)


def test_basic_cesg_above_accrued_room_refused():
    # Born 2020: 7 x $500 = $3,500 of room through 2026.
    _refused(_doc({"ca": "2020-05-01"}, [_beneficiary("ca", contributions=20000.0, basic=3500.01)]))
    _doc_ok = _doc({"ca": "2020-05-01"}, [_beneficiary("ca", contributions=20000.0, basic=3500.0)])
    ic.to_internal_config(_doc_ok)


def test_lifetime_maxima_refused():
    _refused(_doc({"ca": "2008-05-01"}, [_beneficiary("ca", contributions=40000.0,
                                                      basic=7000.0, additional=200.01)]))
    _refused(_doc({"ca": "2010-05-01"}, [_beneficiary("ca", contributions=40000.0, qesi=3600.01)]))
    _refused(_doc({"ca": "2010-05-01"}, [_beneficiary("ca", contributions=40000.0, clb=2000.01)]))


def test_pre_15_above_total_refused():
    _refused(_doc({"ca": "2010-05-01"}, [_beneficiary("ca", contributions=1000.0, before_15=1000.01)]))


def test_impossible_years_with_100_refused():
    # Born 2020: completed years 2020..2025 = 6 possible.
    _refused(_doc({"ca": "2020-05-01"}, [_beneficiary("ca", contributions=5000.0, years=7)]))
    ic.to_internal_config(_doc({"ca": "2020-05-01"}, [_beneficiary("ca", contributions=5000.0, years=6)]))
    # $300 before 15 cannot fund four $100 years.
    _refused(_doc({"ca": "2010-05-01"}, [_beneficiary("ca", contributions=300.0, years=4)]))


def test_positive_balance_with_no_principal_refused():
    _refused(_doc({"ca": "2014-05-01"}, [_beneficiary("ca")], balance=1000.0))


# ── The schema contract ─────────────────────────────────────────────────────

def _beneficiary_item_schema():
    import json
    with open(os.path.join(_ROOT, "schema", "countries", "canada", "input_schema.json")) as f:
        overlay = json.load(f)

    def find(node):
        if isinstance(node, dict):
            if "resp" in node.get("properties", {}) and "beneficiaries" in \
                    node["properties"]["resp"].get("properties", {}):
                return node["properties"]["resp"]["properties"]["beneficiaries"]["items"]
            for v in node.values():
                hit = find(v)
                if hit is not None:
                    return hit
        elif isinstance(node, list):
            for v in node:
                hit = find(v)
                if hit is not None:
                    return hit
        return None
    return find(overlay)


def test_beneficiary_schema_requires_every_field_without_defaults():
    item = _beneficiary_item_schema()
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {
        "person", "contributions_total", "contributions_before_age_15",
        "cesg_basic_received", "cesg_additional_received", "qesi_received",
        "clb_received", "years_with_100_before_age_15"}
    assert set(item["properties"]) == set(item["required"])

    def has_default(node):
        if isinstance(node, dict):
            return "default" in node or any(has_default(v) for v in node.values())
        if isinstance(node, list):
            return any(has_default(v) for v in node)
        return False
    assert not has_default(item), "a beneficiary figure must never carry a default (DP#32)"


@pytest.mark.parametrize("missing", [
    "person", "contributions_total", "contributions_before_age_15",
    "cesg_basic_received", "cesg_additional_received", "qesi_received",
    "clb_received", "years_with_100_before_age_15"])
def test_schema_refuses_a_beneficiary_missing_any_field(missing):
    doc = _two_child_doc()
    del next(a for a in doc["accounts"] if a["kind"] == "resp")["resp"]["beneficiaries"][0][missing]
    with pytest.raises(ContractValidationError):
        contract_schema.validate_contract(doc)


def test_schema_refuses_unknown_field_and_the_old_list_of_ids_shape():
    doc = _two_child_doc()
    resp = next(a for a in doc["accounts"] if a["kind"] == "resp")["resp"]
    resp["beneficiaries"][0]["statement_date"] = "2026-01-01"
    with pytest.raises(ContractValidationError):
        contract_schema.validate_contract(doc)
    resp["beneficiaries"] = ["ca", "cb"]
    with pytest.raises(ContractValidationError):
        contract_schema.validate_contract(doc)


def test_shipped_example_validates_and_sums():
    example = tic._load_example()
    contract_schema.validate_contract(example)
    resp = next(a for a in example["accounts"] if a["kind"] == "resp")["resp"]
    bens = resp["beneficiaries"]
    assert sum(b["contributions_total"] for b in bens) == resp["contributions_total"] == 32000
    assert sum(b["cesg_basic_received"] + b["cesg_additional_received"] for b in bens) \
        == resp["cesg_received"] == 6400
    assert sum(b["qesi_received"] for b in bens) == resp["qesi_received"] == 3200
    assert sum(b["clb_received"] for b in bens) == resp["clb_received"] == 0
    cfg = ic.to_internal_config(tic._two_generation_subset(example))
    children = {c["id"]: c for c in cfg["family"]["children"]}
    for b in bens:
        assert children[b["person"]]["resp_history"]["cesg_basic_received"] == b["cesg_basic_received"]


# ── The $50,000 lifetime contribution limit ─────────────────────────────────

# resp_pct low enough that the strategy share (not the room-aware cap) sets
# the year's RESP total, so the run with the limit patched off allocates the
# SAME total and only the redirect differs.
_NEAR_LIMIT_RESP_PCT = 0.15


def _near_limit_family():
    # ca (14) declared at $49,000 of the $50,000 limit; cb (8) has room. The
    # even split hands ca more than its last $1,000.
    return _doc({"ca": "2012-05-01", "cb": "2018-05-01"},
                [_beneficiary("ca", contributions=49000.0, basic=4000.0),
                 _beneficiary("cb", contributions=4000.0, basic=500.0)])


@pytest.mark.parametrize("mode", MODES)
def test_lifetime_contribution_limit_redirects_excess(mode):
    doc = _near_limit_family()
    results = _run(doc, mode, resp_pct=_NEAR_LIMIT_RESP_PCT)
    config = _config(doc, mode)
    ia = [c["id"] for c in config.children].index("ca")
    assert results[0].resp_contribution_redirected > 0
    for r in results:
        assert max(r.resp_lifetime_contributions) <= 50000.0 + 1e-9
        # What the RESP received is exactly what was credited to the children.
        assert r.contributions["resp"] == pytest.approx(sum(r.resp_contributions_paid), abs=0.01)
    assert results[0].resp_lifetime_contributions[ia] == pytest.approx(50000.0)
    assert results[1].resp_contributions_paid[ia] == 0.0


@pytest.mark.parametrize("mode", MODES)
def test_redirected_excess_is_conserved_in_non_reg(mode, monkeypatch):
    doc = _near_limit_family()
    limited = _run(doc, mode, resp_pct=_NEAR_LIMIT_RESP_PCT)[0]
    # RESPCalculator is a dataclass, so the limit is an instance field: patch
    # the calculator the engine's adapter builds.
    monkeypatch.setattr(CanadaAdapter, "create_resp_calculator",
                        lambda self: RESPCalculator(RESP_LIFETIME_CONTRIBUTION_LIMIT=1e12))
    unlimited = _run(doc, mode, resp_pct=_NEAR_LIMIT_RESP_PCT)[0]
    assert limited.resp_contribution_redirected > 0
    assert unlimited.resp_contribution_redirected == 0
    moved = unlimited.contributions["resp"] - limited.contributions["resp"]
    assert moved == pytest.approx(limited.resp_contribution_redirected, abs=0.01)
    assert limited.contributions["non_reg"] - unlimited.contributions["non_reg"] \
        == pytest.approx(moved, abs=0.01)
    assert sum(limited.contributions.values()) == pytest.approx(
        sum(unlimited.contributions.values()), abs=0.01)


def test_declared_contributions_above_limit_disclosed_and_refused():
    doc = _doc({"ca": "2012-05-01", "cb": "2018-05-01"},
               [_beneficiary("ca", contributions=51000.0, basic=4000.0),
                _beneficiary("cb", contributions=4000.0, basic=500.0)])
    cfg = ic.to_internal_config(doc)
    active = {a.id: a for a in model_fidelity.active_approximations(cfg)}
    entry = active["resp_declared_contributions_exceed_lifetime_limit"]
    findings = entry.findings_for(model_fidelity.FidelityContext(cfg=cfg))
    assert findings and "child_a" in findings[0] and "1,000.00" in findings[0]
    results = _run(doc)
    ia = [c["id"] for c in _config(doc).children].index("ca")
    assert all(r.resp_contributions_paid[ia] == 0.0 for r in results)
    assert results[0].resp_contribution_redirected > 0


# ── CLB, QESI, and seeding parity ───────────────────────────────────────────

def test_clb_is_a_grant_but_not_in_the_cesg_lifetime_counter():
    """$6,000 of CESG + $2,000 of CLB: CLB joins the grant bucket (not
    earnings), and the child can still receive up to $1,200 more CESG."""
    doc = _doc({"ca": "2012-05-01"},
               [_beneficiary("ca", contributions=30000.0, basic=5000.0, additional=1000.0,
                             clb=2000.0)])
    cfg = ic.to_internal_config(doc)
    comp = cfg["accounts"]["resp_composition"]
    assert comp["total_cesg_received"] == 8000.0
    assert comp["investment_earnings"] == 0.0
    results = _run(doc)
    total_cesg = sum(r.resp_cesg_paid[0] for r in results)
    assert 0 < total_cesg <= 1200.0 + 1e-9


@pytest.mark.parametrize("mode", MODES)
def test_quebec_qesi_lifetime_seeded(mode):
    capped = _doc({"ca": "2014-05-01"},
                  [_beneficiary("ca", contributions=30000.0, basic=3000.0, qesi=3600.0)])
    results = _run(capped, mode)
    assert all(r.resp_qesi_paid[0] == 0 for r in results)
    assert results[0].resp_cesg_paid[0] > 0  # eligible, contributions flow

    near = _doc({"ca": "2014-05-01"},
                [_beneficiary("ca", contributions=30000.0, basic=3000.0, qesi=3500.0)])
    near_results = _run(near, mode)
    assert 0 < sum(r.resp_qesi_paid[0] for r in near_results) <= 100.0 + 1e-9


def test_ontario_child_gets_no_qesi_because_of_province():
    for qesi in (3600.0, 0.0):
        doc = _doc({"ca": "2014-05-01"},
                   [_beneficiary("ca", contributions=30000.0, basic=3000.0, qesi=qesi)],
                   province="ontario")
        results = _run(doc)
        assert results[0].resp_cesg_paid[0] > 0
        assert all(r.resp_qesi_paid[0] == 0 for r in results)


def _seeded(child):
    return (child.name, child.birth_year, child.province, child.total_contributions,
            child.total_basic_cesg_received, child.total_additional_cesg_received,
            child.total_qesi_received, child.total_clb_received, child.total_before_age_15,
            child.prior_years_with_100_before_age_15, child.grant_history)


def test_optimizer_and_simulation_seed_children_identically():
    from optimizer import GridOptimizer
    doc, _a, _b = _family_plan()
    config = _config(doc)
    sim = FamilySimulation(config, adapter=CanadaAdapter(config), strategy=_strategy(0.5))
    opt = GridOptimizer(config)
    ctx = opt._build_context(config, _strategy(0.5), False, False, 0.0,
                             initial_state_for_run(config))
    assert [_seeded(c) for c in ctx.resp_children] == [_seeded(c) for c in sim.resp_children]
    assert all(c.grant_history is not None for c in ctx.resp_children)


def test_one_constructor_one_cesg_path():
    """DP#9: the deleted spellings stay deleted, and both engines build
    children through the adapter's shared constructor."""
    out = subprocess.run(
        ["git", "grep", "-nE", r"calculate_cesg_with_catchup|unused_cesg_room|create_resp_child\(|resp_annual_match_cap",
         "--", "*.py", ":!tests/*"],
        cwd=_ROOT, capture_output=True, text=True)
    assert out.stdout == "", out.stdout
    ctor = subprocess.run(["git", "grep", "-lE", r"RESPChild\(", "--", "*.py", ":!tests/*"],
                          cwd=_ROOT, capture_output=True, text=True)
    assert ctor.stdout.split() == ["countries/canada/resp_rules.py"], ctor.stdout
    for rel in ("simulation.py", "optimizer.py"):
        with open(os.path.join(_ROOT, rel)) as f:
            assert "create_resp_children(" in f.read(), rel


@pytest.mark.parametrize("mode", MODES)
def test_run_twice_is_identical(mode):
    """A second run() must start from freshly seeded children, not from the
    counters the first run advanced. run() publishes its final SimState to
    the instance (a pre-existing contract), so the opening state is restored
    before the second run to isolate the RESP children."""
    doc, _a, _b = _family_plan()
    sim = _sim(doc, mode)
    opening = sim._state
    first = sim.run()
    sim._state = opening
    second = sim.run()
    for a, b in zip(first, second):
        assert a.resp_cesg_paid == b.resp_cesg_paid
        assert a.resp_qesi_paid == b.resp_qesi_paid
        assert a.resp_balance == b.resp_balance


# ── Opening-state consistency and the RESP cash-out overlay ─────────────────

def test_partial_declaration_is_refused():
    doc, _a, _b = _family_plan()
    cfg = ic.to_internal_config(doc)
    del cfg["family"]["children"][0]["resp_history"]
    with pytest.raises(ValueError, match="Only some children"):
        initial_state_for_run(SimulationConfig.from_dict(cfg))


def test_per_child_balances_must_sum_to_the_household_balance():
    doc, _a, _b = _family_plan()
    cfg = ic.to_internal_config(doc)
    cfg["accounts"]["resp_current_balance"] += 1.0
    with pytest.raises(ValueError, match="opening RESP balances"):
        initial_state_for_run(SimulationConfig.from_dict(cfg))


@pytest.mark.parametrize("key", ["total_contributions", "total_cesg_received",
                                 "total_qesi_received"])
def test_per_child_buckets_must_sum_to_the_household_composition(key):
    doc, _a, _b = _family_plan()
    cfg = ic.to_internal_config(doc)
    cfg["accounts"]["resp_composition"][key] += 1.0
    with pytest.raises(ValueError, match=key):
        initial_state_for_run(SimulationConfig.from_dict(cfg))


@pytest.mark.parametrize("mode", MODES)
def test_resp_cash_out_overlay_zeroes_per_child_opening(mode):
    doc = _two_child_doc()  # both children young: no EAP drains year 0
    cfg = ic.to_internal_config(doc)
    cfg["assumptions"]["time_step"] = mode
    cashed = apply_overlay(cfg, ScenarioOverlay(label="cash out", resp_cash_out=10000.0))
    config = SimulationConfig.from_dict(cashed)
    canada = initial_state_for_run(config).jurisdiction_state["canada"]
    assert canada["resp_balances"] == [0.0, 0.0]
    assert canada["resp_contributions"] == [0.0, 0.0]
    results = FamilySimulation(config, adapter=CanadaAdapter(config),
                               strategy=_strategy(0.5), free_cash=0.0).run()
    r0 = results[0]
    new_money = (sum(r0.resp_contributions_paid) + sum(r0.resp_cesg_paid)
                 + sum(r0.resp_qesi_paid))
    assert new_money > 0
    # Year 0 holds only this year's contributions and grants, grown one year
    # -- none of the $19,200 that was cashed out.
    growth = r0.resp_balance / new_money - 1.0
    assert 0.0 <= growth < 0.15, (r0.resp_balance, new_money)


# ── model_fidelity disclosures (both sides of each predicate, DP#17) ────────

def _golden_cfg():
    from test_golden_trajectory_581 import golden_household_config
    return golden_household_config()


def _active_ids(cfg):
    return {a.id for a in model_fidelity.active_approximations(cfg)}


def test_split_evenly_disclosed_for_two_children_only():
    two, _a, _b = _family_plan()
    one = _doc({"ca": "2014-05-01"}, [_beneficiary("ca", contributions=1000.0)])
    assert "resp_new_contributions_split_evenly" in _active_ids(ic.to_internal_config(two))
    assert "resp_new_contributions_split_evenly" not in _active_ids(ic.to_internal_config(one))


def test_undeclared_history_disclosed_with_findings():
    golden = _golden_cfg()
    assert "resp_grant_history_not_declared" in _active_ids(golden)
    block = model_fidelity.to_dict(golden)
    entry = next(a for a in block["approximations"] if a["id"] == "resp_grant_history_not_declared")
    assert entry["findings"] and all("no declared RESP" in f for f in entry["findings"])
    assert "resp_new_contributions_split_evenly" in {a["id"] for a in block["approximations"]}
    declared, _a, _b = _family_plan()
    assert "resp_grant_history_not_declared" not in _active_ids(ic.to_internal_config(declared))


def test_family_plan_disclosed_only_for_a_shared_account():
    two, _a, _b = _family_plan()
    one = _doc({"ca": "2014-05-01"}, [_beneficiary("ca", contributions=1000.0)])
    assert "resp_family_plan_earnings_attributed_pro_rata" in _active_ids(ic.to_internal_config(two))
    assert "resp_family_plan_earnings_attributed_pro_rata" not in _active_ids(ic.to_internal_config(one))


def test_over_limit_disclosure_inactive_within_the_limit():
    doc = _near_limit_family()
    assert "resp_declared_contributions_exceed_lifetime_limit" not in \
        _active_ids(ic.to_internal_config(doc))


def test_undeclared_path_matches_legacy_golden_terminal():
    """The golden household declares no history: its terminal total is the
    pinned figure, unchanged by #295."""
    from test_golden_trajectory_581 import _run as golden_run
    assert repr(golden_run(_golden_cfg())[-1].total_assets) == '9709753.139463063'


@pytest.mark.parametrize("mode", MODES)
def test_undeclared_children_get_no_catch_up_allocation(mode):
    """An in-memory config with no history (the legacy path): the allocator
    funds exactly the $2,500 contribution max per eligible child -- no
    carry-forward room is invented for grants that were never declared."""
    cfg = ic.to_internal_config(_two_child_doc())
    for child in cfg["family"]["children"]:
        del child["resp_history"]
        del child["resp_opening"]
    cfg["assumptions"]["time_step"] = mode
    config = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(config, adapter=CanadaAdapter(config), strategy=_strategy(0.5))
    assert all(c.grant_history is None for c in sim.resp_children)
    results = sim.run()
    if mode == "yearly":
        assert results[0].contributions["resp"] == 2500 * 2
    # Basic CESG never exceeds 20% of the $2,500 contribution max.
    assert all(cesg <= 500 for r in results for cesg in r.resp_cesg_paid)


def test_qesi_simplification_disclosed_for_quebec_children_only():
    quebec = _doc({"ca": "2014-05-01"}, [_beneficiary("ca", contributions=1000.0)])
    ontario = _doc({"ca": "2014-05-01"}, [_beneficiary("ca", contributions=1000.0)],
                   province="ontario")
    assert "resp_qesi_accumulated_rights_not_modelled" in _active_ids(ic.to_internal_config(quebec))
    assert "resp_qesi_accumulated_rights_not_modelled" not in _active_ids(ic.to_internal_config(ontario))


def test_resp_caveats_render_only_when_a_child_is_present():
    """Without children no #295 caveat renders (the report says nothing is
    active); a child with no declared history -- named by position when it
    carries neither a name nor an id -- brings the caveats and a finding."""
    base = {"assumptions": {"dollar_basis": "nominal"}}
    quiet = "\n".join(model_fidelity.render_text(base, "min_risk"))
    assert "No registered approximations are active" in quiet
    with_child = dict(base, family={"children": [{"birth_year": 2015}]})
    text = "\n".join(model_fidelity.render_text(with_child, "min_risk"))
    assert "child 1: no declared RESP beneficiary history" in text
    by_id = dict(base, family={"children": [{"id": "cb", "birth_year": 2015}]})
    assert "cb: no declared RESP" in "\n".join(model_fidelity.render_text(by_id, "min_risk"))
