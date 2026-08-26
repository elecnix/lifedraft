"""The ``accounts[]`` namespace: the household's pooled balances and the
per-account overrides layered on top of them.

Two halves:

* **The pots** (``map_account_pots``) -- RESP, LIRA/LIF, LSIF and the
  non-registered portfolio are household singletons in the engine (#643), so N
  declared accounts of one kind sum into ONE pot. The per-owner registered
  balances (rrsp/tfsa/spousal_rrsp/fhsa) do NOT come through here: they belong
  to their owner and are mapped by ``contract_people``.
* **The overrides** (``_map_account_overrides``,
  ``_registered_composition_accounts``) -- a per-account ``expected_return``,
  ``locked_until``, ``mer`` or ``product`` flag is blended, balance-weighted,
  into its pot, so a flagged account grows at its own rate while the rest of
  the pot uses the global one (issues #823/#691/#826/#917).

Absence is a strict no-op throughout: an account declaring none of these
leaves its pot on today's global rate, fully liquid and fee-free (DP#32).
"""
from __future__ import annotations

from typing import Any, Dict, List

from contract_errors import ContractAdaptationError
from contract_people import _active_employment_income, _owner_shares, _people_by_id


# Issue #823: account kinds whose balances the engine grows as one aggregate
# pot in `apply_registered_growth` / `apply_non_reg_growth`. A per-account
# `expected_return` override is blended (balance-weighted) into its pot so a
# flagged account grows at its own rate while the rest of the pot uses the
# global return_model rate. Kinds not in this set (resp/dcpp/dbpp/rrif) are
# either not grown at the equity rate or are out of scope for #823.
_GROWTH_POT_KINDS = frozenset(
    {"rrsp", "spousal_rrsp", "tfsa", "fhsa", "non_reg", "lira", "lif"})


def _resolve_product_rules(product):
    """Issue #826 (DP#7/#10/#12/#16): resolve an account's ``product`` flag to
    the product module's rules (expected_return / locked_until defaults).

    Returns ``None`` when ``product`` is None/empty (a generic account with no
    product-module rules -- today's behaviour). The resolution goes through
    ``countries.canada.fonds_ftq.resolve_product`` (imported lazily so this
    jurisdiction-agnostic mapping module does not import the Canada package at
    module load -- DP#25). An unknown product id raises ``ValueError`` from
    the product module (a typo / not-yet-implemented product must not silently
    fall back to generic -- DP#32).
    """
    if not product:
        return None
    from countries.canada.fonds_ftq import resolve_product as _resolve
    return _resolve(product)


def _map_account_overrides(doc: Dict) -> Dict[str, Any]:
    """Issue #823/#691: collect per-account `expected_return`, `locked_until`
    and `mer` overrides into pot-keyed structures the growth and solvency rules
    read.

    Returns ``{'return_overrides': {kind: {override_balance, weighted_rate_sum}},
    'locked': {kind: [{balance, unlock_age, owner_birth_year}]},
    'mer_drag': {kind: {mer_rate}}}``.

    - ``mer_drag[kind]`` (issue #691/#136): the balance-weighted average MER
      rate of accounts of ``kind`` that declared a `mer` fee. The growth rule
      subtracts this rate from the pot's gross rate: ``net = gross - mer_rate``
      -- so the fee is ``mer_rate * pot_total`` each year, dynamic (not frozen
      at the opening balance). When every MER-flagged account opens at $0,
      ``mer_rate`` falls back to the max declared MER for a SINGLE-flagged pot
      (every account of the kind declares a MER — the pot IS the flagged money),
      or 0.0 for a MIXED pot (non-flagged money coexists — charging it would tax
      money that never declared a fee). A null/absent `mer` records nothing
      (fee-free global rate, golden);
      an explicit 0.0 is recorded (a declared fact, DP#32) but moves no rate.

    - ``return_overrides[kind]``: the summed balance of accounts of ``kind``
      that declared an `expected_return`, plus the balance-weighted rate sum
      (`sum(balance * rate)`). The growth rule blends this into the pot at
      runtime: ``pot_rate = (weighted_rate_sum + (pot_total - override_balance)
      * global) / pot_total``. Accounts without an override contribute at the
      global rate (the default; preserves today's behaviour when no account
      declares one -- golden).
    - ``locked[kind]``: one entry per locked account of ``kind``, carrying its
      balance, its unlock AGE (DP#1: age is date-computed from the owner's
      birth_year, never a hardcoded constant), and the owner's birth_year. The
      solvency rule excludes a locked balance from the liquidation waterfall
      in any year the owner has not yet reached `unlock_age`; after that age
      it is liquid. A `locked_until.date` is converted to the owner's age at
      that date (a date condition and an age condition are the same fact once
      the owner's birth_year is known -- DP#1).

    DP#32: an absent `expected_return` / `locked_until` is null (not zero, not
    a fallback) -- the caller that declares none gets today's global-rate,
    fully-liquid behaviour, which is what keeps the golden invariant unchanged.

    Issue #826 (DP#7/#10/#12/#13): an account carrying a `product` flag (e.g.
    'fonds_ftq') resolves the product module's well-known rules as DEFAULTS
    for expected_return / locked_until -- so a household flags the product
    and does NOT restate its rules. An EXPLICIT account.expected_return /
    account.locked_until on the same account OVERRIDES the product default
    (DP#13: a declared value wins over a fallback). The product defaults feed
    the SAME #823 downstream machinery; this function does not duplicate it.
    """
    people = _people_by_id(doc)
    return_overrides: Dict[str, Dict[str, float]] = {}
    locked: Dict[str, List[Dict[str, Any]]] = {}
    mer_drag: Dict[str, Dict[str, float]] = {}
    # Issue #136: total opening balance of ALL accounts of each growth-pot
    # kind (not just MER-flagged ones). Used to compute mer_rate as
    # weighted_mer_sum / kind_total so the fee is correct in year 1 (the rate
    # times the opening pot equals the frozen weighted_mer_sum) and does not
    # decay in year 2+ (the rate is constant, applied to the current pot).
    # This year-1 equality holds for the weighted-average path; the $0-fallback
    # path (see below) intentionally diverges — it cannot use the weighted
    # average because the numerator is 0, so it uses a different mechanism.
    kind_totals: Dict[str, float] = {}
    # Issue #136: kinds that have at least one NON-MER-declaring account (an
    # account whose `mer` is null/absent). This distinguishes a SINGLE-flagged
    # pot (every account of the kind declares a MER — the pot IS the flagged
    # money) from a MIXED pot (some accounts declare no MER — the pot contains
    # non-flagged money that must NOT be charged the fee).
    has_non_mer: Dict[str, bool] = {}
    # Issue #136: per-kind account-id lists, collected so the mixed-pot-zero
    # fallback (below) can record a model-fidelity disclosure naming the
    # affected accounts. flagged_ids = accounts that declared a `mer`;
    # non_flagged_ids = accounts of the same kind that declared no `mer`.
    # The disclosure fires only when a kind has BOTH (a $0-opening flagged
    # account) and (a non-flagged account) -- the engine's per-owner-pot
    # model cannot route future contributions to the flagged sub-account, so
    # its fee is unmodeled for the whole run and the limitation must surface
    # on every output report (model_fidelity.py, #136).
    flagged_ids: Dict[str, List[str]] = {}
    non_flagged_ids: Dict[str, List[str]] = {}
    mer_mixed_pot: List[Dict[str, Any]] = []
    for acc in doc.get("accounts", []):
        kind = acc["kind"]
        if kind not in _GROWTH_POT_KINDS:
            continue
        amount = acc["balance"]["amount"]
        kind_totals[kind] = kind_totals.get(kind, 0.0) + amount
        # Issue #826 (DP#7/#10/#12/#13): a product flag resolves the
        # product module's well-known rules as DEFAULTS for expected_return /
        # locked_until. An EXPLICIT account.expected_return / account.locked_until
        # wins over the product default (DP#13: a declared value beats a
        # fallback). So the effective values are: explicit if declared, else
        # the product default, else None (today's global-rate / fully-liquid
        # behaviour -- golden when no product is declared either).
        product_rules = _resolve_product_rules(acc.get("product"))
        er = acc.get("expected_return")
        if er is None and product_rules is not None:
            er = product_rules.expected_return
        if er is not None:
            entry = return_overrides.setdefault(kind, {"override_balance": 0.0,
                                                        "weighted_rate_sum": 0.0})
            entry["override_balance"] += amount
            entry["weighted_rate_sum"] += amount * er
        # Issue #691/#136: a per-account MER (management-expense-ratio) fee.
        # The fee is a RATE (mer_rate), not a frozen weighted sum: the growth
        # rule subtracts mer_rate from the pot's gross rate (net = gross -
        # mer_rate) so the fee is mer_rate * pot_total each year — dynamic, not
        # frozen at the opening balance. This fixes two defects:
        #   1. An account that opens at $0 with a declared MER paid ZERO fee
        #      forever (weighted_mer_sum = 0 * mer = 0). Now mer_rate = mer even
        #      when the opening balance is $0, so once contributions fund the
        #      account it pays mer * balance each year (DP#32: the zero was a
        #      silent fallback, not a declared value).
        #   2. A funded account's fee numerator was frozen at load time while
        #      the pot total grew, so the effective fee rate decayed toward
        #      zero. Now mer_rate is a constant rate applied to the current
        #      pot total, so the fee grows proportionally — no decay.
        # DP#32: an explicit 0.0 IS a declared fact (fee-free), recorded here
        # (mer_rate = 0.0) and distinct from a null/absent MER, which records
        # nothing and leaves today's global-rate behaviour untouched (golden).
        # This is the ONE engine-read fee spelling (DP#8); it composes on top
        # of the #823 expected_return blend above.
        #
        # mer_rate is the balance-weighted average of the declared MERs when
        # any MER-flagged account has a non-zero opening balance; when every
        # MER-flagged account opens at $0, mer_rate falls back to the max
        # declared MER for a SINGLE-flagged pot (every account is flagged — the
        # pot IS the flagged money), or 0.0 for a MIXED pot (non-flagged money
        # coexists — charging it would tax money that never declared a fee).
        # See the collapse loop below for the full reasoning.
        mer = acc.get("mer")
        if mer is not None:
            m = mer_drag.setdefault(kind, {"_mer_balance": 0.0,
                                           "_weighted_mer_sum": 0.0,
                                           "_max_mer": 0.0})
            m["_mer_balance"] += amount
            m["_weighted_mer_sum"] += amount * mer
            if mer > m["_max_mer"]:
                m["_max_mer"] = mer
            flagged_ids.setdefault(kind, []).append(acc["id"])
        else:
            # This account declares no MER — it is non-flagged money in its
            # kind's pot. Record that so the $0-fallback (below) can tell a
            # mixed pot (has non-flagged money) from a single-flagged pot
            # (every account is flagged — the pot IS the flagged money).
            has_non_mer[kind] = True
            non_flagged_ids.setdefault(kind, []).append(acc["id"])
        lu = acc.get("locked_until")
        if lu is None and product_rules is not None:
            lu = product_rules.locked_until
        if lu is not None:
            # The owner whose age gates the unlock. FTQ shares are registered
            # (single owner); for a joint owner_ref the first joint holder is
            # the owner of record (#601's owner shape).
            shares = _owner_shares(acc.get("owner"))
            owner_id = next(iter(shares), None)
            owner = people.get(owner_id) if owner_id else None
            if owner is None:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={kind}) declares "
                    f"locked_until but its owner {owner_id!r} is not in the "
                    f"document's people -- the unlock AGE cannot be computed "
                    f"without a birth date (DP#1/DP#32, issue #823)."
                )
            birth_date = owner.get("birth_date")
            if not birth_date:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={kind}) declares "
                    f"locked_until but its owner {owner_id!r} has no "
                    f"birth_date -- the unlock AGE cannot be computed from "
                    f"a missing birth date (DP#1/DP#32, issue #823)."
                )
            owner_birth_year = int(birth_date[:4])
            if "age" in lu and lu["age"] is not None:
                unlock_age = int(lu["age"])
            elif "date" in lu and lu["date"] is not None:
                unlock_age = int(lu["date"][:4]) - owner_birth_year
            else:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} declares locked_until with neither "
                    f"`age` nor `date` (issue #823) -- one is required."
                )
            locked.setdefault(kind, []).append(
                {"balance": amount, "unlock_age": unlock_age,
                 "owner_birth_year": owner_birth_year})
    # Issue #136: collapse the intermediate collection into the final
    # {mer_rate: float} structure the growth rule reads. mer_rate is
    # weighted_mer_sum / kind_total (the opening pot total of ALL accounts of
    # this kind, not just MER-flagged ones). This makes the fee correct in year
    # 1 (mer_rate * opening_pot = weighted_mer_sum) and constant in year 2+
    # (the rate does not decay as the pot grows — the fee is mer_rate *
    # current_pot, which grows proportionally).
    #
    # The $0-fallback (every MER-flagged account opens at $0, so
    # _weighted_mer_sum = 0 and the weighted average is 0/0) splits on whether
    # the pot is SINGLE-flagged or MIXED:
    #
    #   * SINGLE-flagged pot (has_non_mer[kind] is falsy — every account of
    #     the kind declares a MER, so the pot IS the flagged money): mer_rate
    #     = max_mer. This is exact: when B=0 the fee is max_mer * 0 = 0
    #     (correct — B=0 means fee=0 by definition, not a silent zero); once
    #     contributions fund the pot, the fee is max_mer * B (the whole pot is
    #     flagged money). This preserves the #136 fix for defect #1 (silent
    #     zero on a $0-opening single account).
    #
    #   * MIXED pot (has_non_mer[kind] is True — the pot contains non-flagged
    #     money): mer_rate = 0.0. At load time we cannot know what share of
    #     future contributions flows to the flagged vs non-flagged accounts
    #     (the engine tracks per-owner pots, not per-account sub-balances), so
    #     we cannot invent a rate for the whole pot — that would charge the
    #     non-flagged money a fee it never declared (the #136 review's $1.76M
    #     terminal-asset drop). Setting mer_rate = 0.0 is the smallest honest
    #     mechanism: the flagged accounts have B=0, so fee = 0 * anything = 0
    #     (correct — B=0 means fee=0 by definition); the non-flagged money is
    #     not charged (correct — it declared no fee). This is NOT a silent
    #     zero: the flagged accounts genuinely have $0 (the engine's per-owner-
    #     pot model cannot route contributions to a specific $0 sub-account),
    #     so fee=0 is the honest answer, not a formula that produces 0 from a
    #     funded balance. If the household's intent is that ALL future
    #     contributions go to the flagged account, the contract should declare
    #     the MER on the funded account (or declare a single-account pot).
    #
    # An explicit 0.0 MER yields mer_rate = 0.0 via the weighted-average path
    # (weighted_mer_sum = balance * 0.0 = 0.0, so mer_rate = 0.0 / kind_total =
    # 0.0), which is fee-free — a declared fact distinct from null/absent.
    for kind, m in mer_drag.items():
        if m["_mer_balance"] > 0:
            # _mer_balance is a subset of kind_totals (MER-flagged accounts
            # are a subset of all accounts of this kind), so total > 0 here.
            m["mer_rate"] = m["_weighted_mer_sum"] / kind_totals[kind]
        elif has_non_mer.get(kind):
            # MIXED pot: non-flagged money coexists with $0 flagged accounts.
            # Do not charge the non-flagged money (see the block comment above).
            m["mer_rate"] = 0.0
            # Issue #136: record the mixed-pot-zero limitation so model_fidelity
            # can disclose it (#136). The flagged accounts open at $0 and the
            # engine tracks per-owner pots (not per-account sub-balances), so
            # their future share — and thus their fee — is unknowable at load
            # time and unmodeled for the whole run. This is honest (charging the
            # non-flagged money would be wrong) but must not be silent.
            mer_mixed_pot.append({
                "kind": kind,
                "flagged_account_ids": list(flagged_ids.get(kind, [])),
                "non_flagged_account_ids": list(non_flagged_ids.get(kind, [])),
            })
        else:
            # SINGLE-flagged pot: every account of this kind declares a MER,
            # so the pot IS the flagged money. mer_rate = max_mer so the fee
            # is not silently zero once the pot is funded.
            m["mer_rate"] = m["_max_mer"]
        del m["_mer_balance"]
        del m["_weighted_mer_sum"]
        del m["_max_mer"]
    return {"return_overrides": return_overrides, "locked": locked,
            "mer_drag": mer_drag,
            "mer_mixed_pot": mer_mixed_pot}


#: Issue #917: the registered kinds whose composition the engine reads back --
#: exactly the pots PortfolioConfig.registered_wht_drag (#641/#912) and the
#: asset-location optimizer (#473) act on. non_reg is threaded by its own block
#: (its composition reaches the engine through the non-reg after-tax path).
#: Issue #912 added fhsa/lira/lif: registered_wht_drag now reads their holdings
#: and the fhsa/lira_lif growth rules subtract the resulting WHT drag, so
#: threading their composition here lands on a read key (no longer a dead write,
#: DP#18).
_REGISTERED_COMPOSITION_KINDS = ("rrsp", "tfsa", "fhsa", "lira", "lif")


def _blend_registered_holdings(accs: List[Dict]) -> List[Dict]:
    """Combine several same-kind registered accounts' holdings into ONE
    balance-weighted holdings list (issue #917).

    The engine holds a single rrsp/tfsa pot (a couple's two rrsp accounts are
    summed into one balance), so their two composition declarations must blend
    into one. Each account contributes its holdings scaled by its share of the
    pot's total balance, so the blended foreign intensity is the true
    dollar-weighted average -- not the concatenated SUM, which would double a
    per-unit rate when both accounts hold the same product. When every balance
    is 0 the pot is empty and the rate is inert either way; the accounts are
    then weighted equally so the composition stays well-defined (no div-by-0).
    """
    total = sum(a["balance"]["amount"] for a in accs)
    n = len(accs)
    combined: List[Dict] = []
    for acc in accs:
        bal = acc["balance"]["amount"]
        share = (bal / total) if total > 0 else (1.0 / n)
        for h in acc.get("holdings", []):
            combined.append({"product": h["product"], "weight": h["weight"] * share})
    return combined


def _registered_composition_accounts(doc: Dict, products: Dict) -> Dict[str, Dict]:
    """Issue #917: ``{kind: {composition, yield}}`` for every registered pot in
    ``doc`` that declares product holdings, derived from those holdings.

    Registered pots that declare no holdings contribute no entry, so an absent
    or holdings-free contract is a strict no-op (DP#32): the pots keep the flat
    gross rate and no placement decision exists. The product->composition
    derivation reuses ``PortfolioConfig.from_dict``/``to_dict`` (one derivation
    model, DP#9/DP#24) so the composition the WHT drag and the optimizer read is
    the exact one the engine would derive from the same holdings.
    """
    accounts_in: Dict[str, Dict] = {}
    for kind in _REGISTERED_COMPOSITION_KINDS:
        accs = [a for a in doc.get("accounts", [])
                if a["kind"] == kind and a.get("holdings")]
        if not accs:
            continue
        accounts_in[kind] = {"holdings": _blend_registered_holdings(accs)}
    if not accounts_in:
        return {}
    # Lazy countries.canada import (mirrors this adapter's fonds_ftq import):
    # resolving a declared product to its composition IS the jurisdiction's
    # product model, and reusing the engine's own derivation is DP#9.
    from countries.canada.portfolio import PortfolioConfig
    derived = PortfolioConfig.from_dict(
        {"products": products, "accounts": accounts_in}).to_dict()["accounts"]
    return {kind: {"composition": acct["composition"], "yield": acct["yield"]}
            for kind, acct in derived.items()}


def map_account_pots(doc: Dict, as_of: str) -> tuple:
    """The household-pooled account blocks, as
    ``(accounts_cfg, lira_cfg, lsif_cfg, portfolio_cfg)``.

    RESP, LIRA+LIF, LSIF and the non-registered portfolio are household
    SINGLETONS in the engine (#643), so several declared accounts of one kind
    are summed into one pot -- but only when the facts that SELECT the
    applicable rule agree. Where they cannot (two LIRAs on different
    withdrawal-limit rules, two LSIF purchases each carrying their own
    eligibility) the document is refused loudly rather than blended into a
    plausible wrong answer (DP#32)."""
    resp_accounts = [a for a in doc.get("accounts", []) if a["kind"] == "resp"]
    resp_balance = sum(a["balance"]["amount"] for a in resp_accounts)
    accounts_cfg: Dict[str, Any] = {"resp_current_balance": resp_balance}
    if resp_accounts:
        # #647: SUM every RESP account's composition into the one household
        # bucket the engine tracks (SimState has a single resp_contributions/
        # _cesg/_qesi total, split evenly across children -- a #601/#643
        # follow-up, not this PR's job) -- taking only accounts[0] silently
        # dropped every subsequent RESP's contribution/grant history (a real
        # dollar amount, not a rounding nicety: a family with a joint RESP
        # plus a grandparent-funded second RESP lost the second entirely).
        total_contrib = sum(a["resp"]["contributions_total"] for a in resp_accounts)
        total_cesg = sum(a["resp"]["cesg_received"] for a in resp_accounts)
        total_qesi = sum(a["resp"]["qesi_received"] for a in resp_accounts)
        accounts_cfg["resp_composition"] = {
            "total_contributions": total_contrib,
            "total_cesg_received": total_cesg,
            "total_qesi_received": total_qesi,
            "investment_earnings": max(0.0, resp_balance - total_contrib - total_cesg - total_qesi),
        }

    lira_accounts = [a for a in doc.get("accounts", []) if a["kind"] in ("lira", "lif")]
    lira_cfg: Dict[str, Any] = {}
    if lira_accounts:
        # #647: the engine tracks exactly ONE lira/lif pot (household-
        # singleton, #643). A second LIRA/LIF's BALANCE is only safe to sum
        # into that one pot if every account agrees on the facts that
        # SELECT which withdrawal-limit rule applies -- owner birth year
        # (the age-based limit table), jurisdiction, and reference_rate.
        # Silently taking accounts[0] and dropping a second, disagreeing
        # LIRA is exactly #647's bug; silently summing balances under
        # DIFFERENT rules would apply the wrong limit to part of the money.
        # Refuse rather than guess.
        first = lira_accounts[0]
        first_bd = _people_by_id(doc)[first["owner"]].get("birth_date")
        first_birth_year = int(first_bd[:4]) if first_bd else None
        # Issue #708: an elected early-conversion date (lira.conversion_date).
        # Null/absent = no early election; the age-71 mandatory backstop then
        # applies (unchanged behaviour). Two LIRA/LIF accounts must agree on
        # the election (the engine tracks one pot, #643) — refuse rather than
        # silently pick one.
        first_conversion_date = first["lira"].get("conversion_date")
        for acc in lira_accounts[1:]:
            acc_bd = _people_by_id(doc)[acc["owner"]].get("birth_date")
            acc_birth_year = int(acc_bd[:4]) if acc_bd else None
            if (acc_birth_year != first_birth_year
                    or acc["lira"]["jurisdiction"] != first["lira"]["jurisdiction"]
                    or acc["lira"]["reference_rate"] != first["lira"]["reference_rate"]
                    or acc["lira"].get("conversion_date") != first_conversion_date):
                raise ContractAdaptationError(
                    f"Accounts {first['id']!r} and {acc['id']!r} are both "
                    f"kind=lira/lif, but disagree on owner birth year, "
                    f"jurisdiction, reference rate, or conversion date. The "
                    f"engine tracks exactly one LIRA/LIF pot (#643) -- "
                    f"blending two accounts whose withdrawal-limit rules or "
                    f"conversion election genuinely differ would silently "
                    f"apply the wrong rule to part of the money. Cannot "
                    f"represent both."
                )
        # conversion_date is a full date (DP#1); the conversion fires on a
        # calendar-year boundary, so derive the election year from it.
        conversion_year = (int(first_conversion_date[:4])
                           if first_conversion_date else None)
        lira_cfg = {
            "balance": sum(a["balance"]["amount"] for a in lira_accounts),
            "birth_year": first_birth_year,
            "jurisdiction": first["lira"]["jurisdiction"],
            "reference_rate": first["lira"]["reference_rate"],
            # Issue #708: the elected early-conversion calendar year (or
            # None for no early election -> age-71 backstop). Read by
            # simulation_state's canada-state build -> opening_lira_
            # conversion_year -> apply_lira_lif.
            "conversion_year": conversion_year,
            # source_pension_plan/transfer_date are NOT mapped (epic #603
            # Phase 2b finding): lira.source_pension_plan/.transfer_date were
            # already confirmed dead and DELETED from the legacy schema in
            # Phase 2a (zero production readers -- simulation_state.py's
            # lira_cfg handling reads only .balance/.birth_year/.jurisdiction/
            # .reference_rate/.conversion_year). Mapping them here would
            # recreate exactly the "written, not applied" duplicate
            # declaration Phase 2a deleted.
        }

    # #649: an LSIF is either a STANDALONE kind=lsif account, OR held INSIDE
    # an RRSP wrapper -- the Quebec 'REER FTQ', a kind=rrsp/spousal_rrsp
    # account carrying a nested `lsif` sub-object. The two are taxed
    # differently: the wrapper (RRSP) supplies the deduction/deferral/RRIF
    # base, which the RRSP balance mapping above ALREADY applies; the nested
    # lsif leaf adds ONLY the 30% LSIF credit, computed on the declared
    # `lsif.holding_amount` (the LSIF portion of that RRSP -- NOT the whole
    # balance, which also holds other RRSP assets). A standalone lsif account
    # has no wrapper, so its credit is computed on its own balance (unchanged).
    lsif_accounts = [a for a in doc.get("accounts", []) if a["kind"] == "lsif"]
    nested_lsif_accounts = [
        a for a in doc.get("accounts", [])
        if a["kind"] in ("rrsp", "spousal_rrsp") and a.get("lsif") is not None
    ]
    lsif_bearing = lsif_accounts + nested_lsif_accounts
    lsif_cfg: Dict[str, Any] = {}
    if len(lsif_bearing) > 1:
        # #647: each LSIF purchase carries its OWN eligibility facts
        # (purchase date, province, prior redemption, HBP-replacement
        # status) that drive the tax-credit calculation
        # (countries/canada/lsif_credit.py) for that specific purchase --
        # unlike a plain balance, these cannot be blended into one "average"
        # purchase without silently misapplying one purchase's eligibility
        # to another's money. Taking accounts[0] and dropping the rest (the
        # previous behaviour) silently lost real dollars; refuse instead.
        # #649: standalone lsif accounts AND nested-in-RRSP lsif holdings are
        # counted together -- the engine still represents exactly one purchase.
        raise ContractAdaptationError(
            f"Document declares {len(lsif_bearing)} LSIF holdings "
            f"({sorted(a['id'] for a in lsif_bearing)}, standalone kind=lsif "
            f"accounts and/or lsif nested in an RRSP wrapper -- #649). The "
            f"engine's LSIF model (countries/canada/lsif_credit.py) represents "
            f"exactly ONE purchase -- its own purchase date/amount/province "
            f"drive the credit calculation. Blending multiple purchases into "
            f"one would silently apply one purchase's eligibility to the "
            f"other's money (#643). Cannot represent more than one."
        )
    if lsif_bearing:
        acc = lsif_bearing[0]
        if acc["kind"] == "lsif":
            owner = _people_by_id(doc)[acc["owner"]]
            purchase_amount = acc["balance"]["amount"]
        else:
            # #649 nested-in-RRSP (the 'REER FTQ'): the credit is on the
            # declared LSIF portion, which the wrapper's balance cannot supply.
            holding_amount = acc["lsif"].get("holding_amount")
            if holding_amount is None:
                raise ContractAdaptationError(
                    f"Account {acc['id']!r} (kind={acc['kind']}) declares a "
                    f"nested `lsif` block (an LSIF held inside the RRSP wrapper "
                    f"-- the 'REER FTQ', #649) but no `lsif.holding_amount`. "
                    f"The 30% LSIF credit is computed on the LSIF PORTION of "
                    f"the wrapper, which is not the whole RRSP balance and must "
                    f"be declared, not guessed (DP#32)."
                )
            owner_id = next(iter(_owner_shares(acc["owner"])), None)
            owner = _people_by_id(doc)[owner_id]
            purchase_amount = holding_amount
        lsif_cfg = {
            "purchase_amount": purchase_amount,
            "purchase_year": int(acc["lsif"]["purchase_date"][:4]),
            "is_quebec_resident": acc["lsif"]["purchase_province"] == "quebec",
            "prior_redemption": acc["lsif"]["prior_redemption"],
            "employment_income": _active_employment_income(owner, as_of),
            "reference_year_taxable_income": acc["lsif"].get("reference_year_taxable_income"),
            "quebec_carryforward": acc["lsif"]["quebec_carryforward"],
            "is_hbp_replacement": acc["lsif"]["is_hbp_replacement"],
            "federally_registered": acc["lsif"]["federally_registered"],
            "acquisition_date": acc["lsif"].get("acquisition_date"),
            "redeemed_date": acc["lsif"].get("redeemed_date"),
        }

    # #599 follow-up (Phase 2b): non_reg is still a HOUSEHOLD singleton in the
    # internal shape (SimState carries one non_reg_balance/non_reg_acb, not
    # one per owner) -- multiple non_reg accounts (e.g. one per spouse) are
    # summed into that one household total, the same "owner-summed" treatment
    # _map_member already gives rrsp/tfsa. cost_basis sums only the accounts
    # that DECLARE an acb; if every non_reg account has acb=null (unknown),
    # the household cost_basis is None too -- an unknown mixed with a known
    # number is not a number (DP#32: don't fabricate precision that isn't
    # there). Holdings are combined from every account (weights are
    # per-holding fractions of THAT account's balance; blending across
    # accounts as one combined holdings list is the existing PortfolioConfig
    # shape's only representation of "the household's non-reg composition").
    non_reg_accounts = [a for a in doc.get("accounts", []) if a["kind"] == "non_reg"]
    portfolio_cfg: Dict[str, Any] = {}
    if non_reg_accounts:
        declared_acbs = [a["acb"] for a in non_reg_accounts if a.get("acb") is not None]
        portfolio_cfg["accounts"] = {
            "non_reg": {
                "balance": sum(a["balance"]["amount"] for a in non_reg_accounts),
                "cost_basis": sum(declared_acbs) if len(declared_acbs) == len(non_reg_accounts) else None,
                "holdings": [
                    {"product": h["product"], "weight": h["weight"]}
                    for acc in non_reg_accounts for h in acc.get("holdings", [])
                ],
            }
        }
    products = doc["assumptions"]["products"]  # both schema-required (may be {})
    if products:
        portfolio_cfg["products"] = products

    # Issue #917: thread each REGISTERED pot's declared product holdings into
    # portfolio.accounts.{rrsp,tfsa} as a derived composition + yield -- the
    # shape #641's WHT drag (PortfolioConfig.registered_wht_drag) and #473's
    # asset-location optimizer both read. Before this, only non_reg composition
    # crossed this boundary, so a --input contract declaring "US equity in my
    # RRSP" got the flat rate and no placement advice (both no-ops). Unlike
    # non_reg (a household singleton read only through its own after-tax path,
    # so a raw holdings passthrough suffices), the rrsp/tfsa optimizer reads
    # composition + yield off the config verbatim -- so the product->composition
    # derivation has to happen HERE. That derivation is the jurisdiction's
    # product model; reusing PortfolioConfig.from_dict/to_dict (one derivation,
    # DP#9) via the same lazy countries.canada import this adapter already uses
    # for fonds_ftq keeps a single WHT/composition model rather than a second
    # spelling at the boundary. Registered pots that declare no holdings add no
    # entry at all -- a strict no-op (DP#32), byte-identical to today.
    registered_accounts = _registered_composition_accounts(doc, products)
    if registered_accounts:
        portfolio_cfg.setdefault("accounts", {}).update(registered_accounts)
    return accounts_cfg, lira_cfg, lsif_cfg, portfolio_cfg
