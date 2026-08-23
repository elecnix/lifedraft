"""The ``assumptions`` namespace (plus ``household_budget``): the household's
BELIEFS, kept separate from its facts.

Inflation, salary growth, the tax-law overrides, the retirement drawdown
policy, the emergency-reserve policy, the measured living-cost budget -- and
the two reconciliations that exist because a belief must never quietly outrank
a fact:

* ``apply_rate_path_reconciliation`` (#685) -- a ``rate_paths`` entry that
  contradicts a SIGNED liability rate at year zero is reported, and the
  declared rate wins. A rate path describes what the borrowing costs AFTER the
  current term; it cannot reprice a rate the contract has already pinned.
* ``apply_spending_reconciliation`` (#766) -- a GUESSED retirement
  ``spending_target`` sitting far from the MEASURED working-phase
  ``annual_living_costs`` is surfaced on every output surface, because the
  decumulation shortfall it produces would otherwise read as a fact about the
  household rather than an artifact of the guess.

The detection halves (``_reconcile_rate_paths`` in ``contract_liabilities``,
``_reconcile_spending_figures`` here) are pure functions returning records
(DP#3); the ``apply_*`` callers decide how loudly to say it, and
``model_fidelity`` reads the records back off the internal config so every
report surface names the same figures.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError
from contract_liabilities import _reconcile_rate_paths

logger = logging.getLogger(__name__)


def _reconcile_spending_figures(
        doc: Dict, living_costs: Optional[float],
        spending_target: Optional[float]) -> List[Dict]:
    """Flag when the contract's two spending figures disagree materially (#766).

    ``household_budget.annual_living_costs`` is the household's MEASURED (or
    derived) working-phase spend. ``assumptions.retirement.spending_target`` is
    the retirement-phase spend the decumulation drawdown sizes itself to. They
    are not the same quantity -- one is working-life (and excludes debt
    payments), one is retirement (net of tax) -- so they need not be equal: a
    retirement target can legitimately sit above working-life spend (more
    travel) or below it (mortgage paid, children independent).

    But a retirement target that sits FAR from measured working-life spend, with
    no reconciliation, is exactly the #766 defect: a GUESSED retirement figure
    silently outranks a MEASURED living-cost figure, and the decumulation
    shortfall it produces is an artifact of the guess, not of the household's
    finances. This surfaces the disagreement -- both values, the ratio, and each
    leaf's provenance confidence when the sidecar is present -- so every output
    surface that prints a decumulation number also prints the fact that the two
    spending figures the household declared do not agree.

    Returns one record when both figures are present and their ratio falls
    outside [0.75, 1.25] (a +/-25% band -- a heuristic that catches a MATERIAL
    gap without flagging benign small differences). Empty list otherwise.

    Pure (DP#3): no logging, no mutation. ``model_fidelity.spending_figure_conflicts()``
    reads the records back off the internal config so every report surface can
    name the same two figures the load-time warning did.
    """
    if living_costs is None or spending_target is None:
        return []
    if living_costs <= 0:
        return []
    ratio = spending_target / living_costs
    _BAND_LO, _BAND_HI = 0.75, 1.25
    if _BAND_LO <= ratio <= _BAND_HI:
        return []
    provenance = doc.get("provenance")
    provenance = {} if provenance is None else provenance

    def _conf(pointer: str) -> Optional[str]:
        entry = provenance.get(pointer)
        return entry.get("confidence") if isinstance(entry, dict) else None

    return [{
        "living_costs": living_costs,
        "spending_target": spending_target,
        "ratio": ratio,
        # The decumulation sizes to the retirement target, so a guessed target
        # is what biases the shortfall -- 'winner' names what the run uses.
        "winner": "spending_target",
        "living_costs_confidence": _conf("/household_budget/annual_living_costs"),
        "spending_target_confidence": _conf("/assumptions/retirement/spending_target"),
    }]


def map_assumptions(doc: Dict, start_year: int) -> tuple:
    """The internal ``cfg['assumptions']`` block, as
    ``(assumptions_cfg, resp_account_settings)``.

    ``resp_account_settings`` is the handful of RESP BELIEFS the engine reads
    off ``cfg['accounts']`` rather than off ``cfg['assumptions']``
    (``resp_study_start_age`` and friends -- issue #578); the caller merges
    them into the accounts block, so each key keeps exactly one spelling, on
    the side the engine actually reads it from."""
    resp_account_settings: Dict[str, Any] = {}
    assumptions = doc["assumptions"]
    # default_non_reg_yield/capital_gains_inclusion are explicitly nullable
    # beliefs (null = "no coarse fallback"/"use the code table"); a genuine
    # 0.0 must NOT be coerced to the legacy default (DP#32) -- `is None`,
    # never `or`.
    default_yield = assumptions["default_non_reg_yield"]
    cg_inclusion = assumptions["tax_law_overrides"]["capital_gains_inclusion"]
    assumptions_cfg: Dict[str, Any] = {
        "inflation": assumptions["inflation"],
        "salary_growth": assumptions["salary_growth"],
        "start_year": start_year,
        "time_step": assumptions["time_step"],
        "non_reg_yield_rate": 0.02 if default_yield is None else default_yield,
        "capital_gains_inclusion": 0.50 if cg_inclusion is None else cg_inclusion,
        "frozen_brackets": assumptions["tax_law_overrides"]["frozen_brackets"],
        # #585 (epic #603 Phase 2b finding): the document's own units --
        # root-level currency/dollars/real_base_year -- had no path into the
        # internal shape at all, so model_fidelity.describe_units (which
        # reads assumptions.currency/.dollar_basis/.base_year to declare
        # nominal-vs-real on every output surface) silently fell back to
        # its defaults for every contract-sourced run. Mapped for real here.
        "currency": doc["currency"],
        "dollar_basis": doc["dollars"],
    }
    if doc["dollars"] == "real":
        assumptions_cfg["base_year"] = doc["real_base_year"]
    resp_beliefs = assumptions.get("resp")
    if resp_beliefs:
        assumptions_cfg["resp_eap_tax_rate"] = resp_beliefs["eap_tax_rate"]
        assumptions_cfg["resp_eap_taxable_portion"] = resp_beliefs["eap_taxable_portion"]
        # issue #578 (epic #603 Phase 2b): these three used to have NO home
        # anywhere in the contract schema at all -- worse than dead, they
        # were unrepresentable, so a contract document could never override
        # SimulationConfig's defaults (study_start_age=18/duration=4/
        # used_for_education=True). Real config fields, mapped into
        # `accounts` (SimulationConfig.from_dict reads them off
        # `accounts.resp_study_start_age` etc., not `assumptions.*`).
        resp_account_settings["resp_study_start_age"] = resp_beliefs["study_start_age"]
        resp_account_settings["resp_study_duration_years"] = resp_beliefs["study_duration_years"]
        resp_account_settings["resp_used_for_education"] = resp_beliefs["used_for_education"]
    return assumptions_cfg, resp_account_settings


def apply_rate_path_reconciliation(assumptions_cfg: Dict[str, Any], assumptions: Dict,
                                  mortgage: Optional[Dict],
                                  heloc: Optional[Dict]) -> None:
    """Reconcile ``assumptions.rate_paths`` against the SIGNED liability rates,
    recording and warning about every year-zero contradiction.

    The declared rate always wins. The records are written onto
    ``assumptions_cfg['rate_path_conflicts']`` and read back by
    ``model_fidelity.rate_path_conflicts()``, so every report surface names the
    same figures the load-time warning does -- a warning scrolls off, a report
    does not."""
    # ── issue #685: the belief does NOT get to overwrite the signed rate ──
    #
    # This block used to read
    #
    #     heloc_path = assumptions["rate_paths"]["heloc"]
    #     if heloc_path["type"] == "fixed":
    #         assumptions_cfg["heloc_rate"] = heloc_path["rate"]
    #
    # and that single line is #685. `assumptions.heloc_rate` is the
    # DECISION channel — `resolve_heloc_rate`'s tier 1, written by
    # `optimize.apply_anchor_preset` for a deliberate, labelled hypothetical
    # ("what if the HELOC also reprices on renewal", DP#5) — and it outranks
    # `property.heloc_rate`, the household's OWN signed rate (#654's
    # canonical spelling), precisely so that such a decision is not shadowed
    # by it. Piping a rate_paths BELIEF through that same channel handed the
    # belief the decision's authority: a stale rate_paths block silently
    # outranked a signed contract, and no output said so.
    #
    # So the contract loader no longer writes the decision channel at all.
    # The signed rate reaches the engine and every optimizer/report consumer
    # by the one spelling that is a fact — property.heloc_rate, mapped from
    # liabilities[kind=heloc].rate above. `rate_paths` keeps its real job
    # (what the borrowing costs AFTER the current term; there is no
    # year-over-year rate PATH in the engine to consume that yet) and gains
    # a real one it never had: it is now RECONCILED against the contract,
    # and a disagreement about year zero is reported instead of silently won.
    rate_conflicts = _reconcile_rate_paths(
        assumptions["rate_paths"], {"mortgage": mortgage, "heloc": heloc})
    if rate_conflicts:
        # Read back by model_fidelity.rate_path_conflicts() so the JSON/HTML/
        # console reports name the same figures this warning does -- a warning
        # scrolls off; the report does not.
        assumptions_cfg["rate_path_conflicts"] = rate_conflicts
        for c in rate_conflicts:
            logger.warning(
                "CONTRADICTION (#685): assumptions.rate_paths.%s asserts %.2f%% for "
                "the CURRENT year, but liability %r (kind=%s) declares a SIGNED rate "
                "of %.2f%%. A signed rate is a FACT; a rate path is a BELIEF -- the "
                "DECLARED %.2f%% WINS and is what this run charges. rate_paths "
                "describes what this borrowing costs AFTER the current term ends; it "
                "cannot reprice a rate the contract has already pinned. Fix the "
                "document: set rate_paths.%s to the signed rate (or drop it) unless "
                "you meant to state a renewal belief -- in which case it disagrees "
                "with your own contract at year zero.",
                c["liability_kind"], c["believed_rate"] * 100, c["liability_id"],
                c["liability_kind"], c["declared_rate"] * 100, c["declared_rate"] * 100,
                c["liability_kind"],
            )


def map_retirement(doc: Dict) -> Dict[str, Any]:
    """``assumptions.retirement`` (plus the OAS tax-law override) -> the
    internal ``cfg['retirement']`` block. Every leaf is carried only when
    declared, so a household stating none of them is byte-identical to the
    engine's own defaults (DP#13/DP#32)."""
    assumptions = doc["assumptions"]
    oas_override = assumptions["tax_law_overrides"].get("oas")
    retirement_cfg: Dict[str, Any] = assumptions["retirement"]  # schema-required
    retirement_out: Dict[str, Any] = {}
    if retirement_cfg.get("drawdown_order"):
        retirement_out["drawdown_order"] = retirement_cfg["drawdown_order"]
    if retirement_cfg.get("spending_target") is not None:
        retirement_out["spending_target"] = retirement_cfg["spending_target"]
    if retirement_cfg.get("net_replacement_rate") is not None:
        # epic #603 Phase 2b finding: real, consumed field (simulation.py's
        # ret.get('net_replacement_rate', DEFAULT_NET_REPLACEMENT_RATE))
        # that this mapping was silently dropping -- schema-declared,
        # engine-read, but never reached. Wired for real here.
        retirement_out["net_replacement_rate"] = retirement_cfg["net_replacement_rate"]
    # Issue #1009: the opt-in die-with-(near)-zero drawdown mode
    # (simulation_rules.apply_retirement_income reads
    # ret.get('liquidate_to_target')). Absence-safe: carried only when the
    # contract declares it (a bool leaf -- absent == off, never coerced), so a
    # household that does not opt in is byte-identical (DP#32).
    if retirement_cfg.get("liquidate_to_target") is not None:
        retirement_out["liquidate_to_target"] = bool(
            retirement_cfg["liquidate_to_target"])
    # retirement_cfg["drawdown_tax_mode"] is NOT mapped: issue #579 deleted
    # the gross-drawdown code path entirely (the 'gross' vs 'net' switch has
    # zero readers anywhere in the engine today, confirmed by grep) -- the
    # schema keeps the field for its own documentation value ("gross exists
    # only for back-testing"), but there is nothing left for a mapped value
    # to switch.
    if oas_override:
        if oas_override.get("disabled"):
            logger.warning(
                "assumptions.tax_law_overrides.oas.disabled=true has NO legacy "
                "equivalent (issue #592 -- the legacy engine cannot represent "
                "'no OAS' other than an annual_max_override of 0, which is "
                "exactly #592's bug). Mapping to oas_annual_max=0 reproduces "
                "the known gap rather than truly disabling OAS; fixing this "
                "requires #592 on the engine side, out of Phase 1's scope."
            )
            retirement_out["oas_annual_max"] = 0
        elif oas_override.get("annual_max_override") is not None:
            retirement_out["oas_annual_max"] = oas_override["annual_max_override"]
    return retirement_out


def map_household_budget(doc: Dict) -> Dict[str, Any]:
    """``household_budget`` -> the internal budget block: the MEASURED
    working-phase living-cost scalar, its discretionary split (#761), and any
    dated finite-term expense segments layered on top (#760/#882).

    The three are not independently optional: declaring a discretionary
    fraction or a dated segment without the measured base scalar is a
    contradiction, refused loudly rather than defaulted to zero (DP#32)."""
    # Issue #679: household_budget is optional (DP#16 -- see the schema's
    # own description) and its leaf is nullable, so it is mapped only when
    # actually measured (explicit presence test, never a truthiness/`or`
    # coercion -- DP#32).
    household_budget_cfg = doc.get("household_budget")
    household_budget_out: Dict[str, Any] = {}
    if household_budget_cfg:
        if household_budget_cfg.get("annual_living_costs") is not None:
            household_budget_out["living_costs"] = household_budget_cfg["annual_living_costs"]
        # Issue #761: the discretionary/non-discretionary split of the
        # measured living-cost scalar. Optional (DP#16): absent reproduces
        # today's behaviour exactly (the whole scalar is rigid). Explicit
        # presence test, never a truthiness/`or` coercion -- 0.0 is a real,
        # declarable answer ("all my spending is non-discretionary"),
        # distinct from omitting the field (DP#32).
        if household_budget_cfg.get("discretionary_fraction") is not None:
            # DP#32: a discretionary fraction of NOTHING is a contradiction --
            # the fraction is a share OF annual_living_costs, so declaring it
            # without a measured scalar must fail loudly, never default the
            # missing scalar to zero (which would silently make the split a
            # no-op) or to the full amount. The two halves of the split are
            # not independently optional.
            if household_budget_cfg.get("annual_living_costs") is None:
                raise ContractAdaptationError(
                    f"household_budget.discretionary_fraction = "
                    f"{household_budget_cfg['discretionary_fraction']!r} is "
                    f"declared, but household_budget.annual_living_costs is "
                    f"not measured. The discretionary fraction is a SHARE OF the "
                    f"annual living-cost scalar -- declaring one without the "
                    f"other is a contradiction, not a default-to-zero (DP#32). "
                    f"Measure annual_living_costs (12 months of bank/credit "
                    f"statements) before declaring how much of it is "
                    f"discretionary, or omit discretionary_fraction to keep the "
                    f"whole scalar rigid (issue #761)."
                )
            household_budget_out["discretionary_fraction"] = (
                household_budget_cfg["discretionary_fraction"])

        # Issue #760: dated, finite-term living-cost segments layered on top of
        # the perpetual annual_living_costs scalar (a private-school tuition
        # that ENDS when a child ages out, childcare, a term expense that
        # stops). Optional (DP#16): absent reproduces today's behaviour exactly
        # (the golden invariant does not move, DP#32). Mapped into the internal
        # household_budget shape the fold reads
        # (simulation_rules.apply_solvency, via SimulationConfig.expense_segments)
        # so the segments reach a decision, not sit as dead leaves (DP#18).
        segments_in = household_budget_cfg.get("expense_segments")
        if segments_in:
            # DP#32: a dated living-cost segment is a share ON TOP OF the
            # measured base scalar -- declaring one without annual_living_costs
            # must fail loudly (the solvency module is engaged by the base;
            # DP#16), never default the missing base to zero. Same non-
            # independence as the discretionary split above.
            if household_budget_cfg.get("annual_living_costs") is None:
                raise ContractAdaptationError(
                    f"household_budget.expense_segments is declared "
                    f"({len(segments_in)} segment(s)), but household_budget."
                    f"annual_living_costs is not measured. A dated expense "
                    f"segment is an ADDITIONAL living cost layered on the "
                    f"measured base scalar -- declaring one without the base is "
                    f"a contradiction, not a default-to-zero (DP#32). Measure "
                    f"annual_living_costs (12 months of bank/credit statements) "
                    f"before declaring dated segments on top of it (issue #760)."
                )
            expense_segments: List[Dict[str, Any]] = []
            for seg in segments_in:
                # DP#32: a zero/negative window is not silently treated as $0 --
                # it is refused. `to` is nullable (null = perpetual, the explicit
                # spelling), but a NON-null `to` on or before `from` is a
                # contradiction (an expense that ends before it starts).
                if seg["to"] is not None and seg["to"] <= seg["from"]:
                    raise ContractAdaptationError(
                        f"household_budget expense_segment "
                        f"{seg['description']!r} declares to={seg['to']!r} on or "
                        f"before from={seg['from']!r} -- an expense that ends "
                        f"before it starts is an empty window. Declare to strictly "
                        f"after from, or to: null for a perpetual segment "
                        f"(issue #760); silently treating it as $0 is the DP#32 "
                        f"trap."
                    )
                # Issue #882: OPTIONAL intra-year seasonality. `active_months` (a
                # subset of 1..12) spends the ANNUAL `amount` in equal shares
                # across only those months (heating Nov-Mar, a property-tax bill
                # each July); absence means active every day of the window
                # (byte-for-byte #760, DP#32). Its structural constraints -- a
                # non-empty list of unique months in 1..12 -- are enforced
                # declaratively by the schema (minItems/uniqueItems/range), which
                # to_internal_config runs before this loop, so an empty or
                # duplicated list is already refused loudly upstream (DP#32); no
                # redundant guard here (DP#9).
                active_months = seg.get("active_months")
                mapped = {
                    "description": seg["description"],
                    "amount": seg["amount"],
                    # Dates travel through as ISO strings; the fold rule parses
                    # them once per year (simulation_rules._expense_segment_
                    # contribution_in_year), the same string->date convention
                    # apply_installments uses for start_date.
                    "from": seg["from"],
                    "to": seg["to"],
                    "non_discretionary": seg["non_discretionary"],
                }
                # Only carry the key when declared -- an absent active_months
                # round-trips to absent, never a fabricated all-months block
                # (DP#24/DP#32).
                if active_months is not None:
                    mapped["active_months"] = active_months
                expense_segments.append(mapped)
            household_budget_out["expense_segments"] = expense_segments
    return household_budget_out


def map_emergency_reserve(doc: Dict, spouse_id: Optional[str]) -> Dict[str, Any]:
    """``assumptions.emergency_reserve`` -> the internal reserve policy
    (issue #688).

    ``held_in`` names an ACCOUNT ID, but the engine tracks one pot per account
    KIND (#643), so the id is resolved to its kind here -- the mapping layer is
    exactly where a document-level reference becomes an engine-level one. An id
    that names no declared account is a typo, not a reserve of zero, and the
    two must never be confused (DP#32)."""
    assumptions = doc["assumptions"]
    # Issue #688: the emergency-reserve POLICY. `held_in` names an ACCOUNT ID,
    # but this engine tracks one pot per account KIND (#643), so the id is
    # resolved to its kind here -- the mapping layer is exactly where a
    # document-level reference becomes an engine-level one. An id that names
    # no declared account is refused rather than silently dropped: a reserve
    # pointed at an account that does not exist is not a reserve of zero, it
    # is a typo, and the two must never be confused (DP#32).
    reserve_cfg = assumptions.get("emergency_reserve")
    reserve_out: Dict[str, Any] = {}
    if reserve_cfg:
        held_in_id = reserve_cfg.get("held_in")
        held_in_kind = None
        if held_in_id is not None:
            hosts = [a for a in doc.get("accounts", []) if a["id"] == held_in_id]
            if not hosts:
                raise ContractAdaptationError(
                    f"assumptions.emergency_reserve.held_in = {held_in_id!r}, but no "
                    f"account with that id is declared. The reserve is a cash sleeve "
                    f"carved out of a real account (#688) -- it cannot be held in an "
                    f"account that does not exist. Declared account ids: "
                    f"{sorted(a['id'] for a in doc.get('accounts', []))}."
                )
            held_in_kind = hosts[0]["kind"]
            # #643: the engine holds ONE pot per kind, and the primary's TFSA
            # and the spouse's are separate pots. Attribute a TFSA reserve to
            # whichever spouse's pot actually owns the account.
            if held_in_kind == "tfsa" and hosts[0].get("owner") == spouse_id:
                held_in_kind = "tfsa_spouse"
        reserve_out = {
            "target_months": reserve_cfg["target_months"],   # schema-required
            "rate": reserve_cfg["rate"],                     # schema-required
            "instrument": reserve_cfg["instrument"],         # schema-required
            "held_in": held_in_kind,
        }
    return reserve_out


def apply_spending_reconciliation(assumptions_cfg: Dict[str, Any], doc: Dict,
                                  household_budget_out: Dict[str, Any],
                                  retirement_out: Dict[str, Any]) -> None:
    """Reconcile the contract's two spending figures and record any material
    disagreement onto ``assumptions_cfg['spending_figure_conflicts']``
    (issue #766). Mirrors ``apply_rate_path_reconciliation``."""
    # Issue #766: the contract's two spending figures -- the MEASURED
    # working-phase `household_budget.annual_living_costs` and the retirement
    # `assumptions.retirement.spending_target` the decumulation sizes to --
    # are consumed by different subsystems and never compared. A guessed
    # retirement target can silently outrank a measured living-cost figure and
    # produce a spurious decumulation shortfall. Reconcile them here (the only
    # place both are visible at once) and record the disagreement so
    # model_fidelity.spending_figure_conflicts() can name both figures on every
    # output surface that prints a decumulation number. Mirrors #685's
    # _reconcile_rate_paths. The band is a heuristic, not a requirement that
    # they be equal -- a retirement target legitimately differs from working-
    # life spend; a MATERIAL gap is the defect.
    spending_conflicts = _reconcile_spending_figures(
        doc, household_budget_out.get("living_costs"),
        retirement_out.get("spending_target"))
    if spending_conflicts:
        assumptions_cfg["spending_figure_conflicts"] = spending_conflicts
        for c in spending_conflicts:
            lc = c["living_costs_confidence"]
            lc_conf = "(no provenance entry)" if lc is None else lc
            st = c["spending_target_confidence"]
            st_conf = "(no provenance entry)" if st is None else st
            logger.warning(
                "CONTRADICTION (#766): the contract declares two spending figures "
                "that disagree by a factor of %.2fx: household_budget."
                "annual_living_costs = %.0f (provenance: %s) vs. assumptions."
                "retirement.spending_target = %.0f (provenance: %s). They are not "
                "the same quantity (working-life vs. retirement), so a small gap is "
                "fine -- but a gap this large means a GUESSED retirement target is "
                "silently outranking a MEASURED living-cost figure, and the "
                "decumulation shortfall it produces is an artifact of the guess, not "
                "of the household's finances. The decumulation sizes to the retirement "
                "target. Reconcile them, or confirm the gap is intended.",
                c["ratio"], c["living_costs"], lc_conf, c["spending_target"], st_conf,
            )
