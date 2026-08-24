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
  ``locked_until``, ``mer``, ``deductible_management_fee_annual`` or
  ``product`` flag reaches the engine: rate/fee overrides are blended,
  balance-weighted, into the pot (issues #823/#691/#826/#917), and the
  separately-charged s.20(1)(e) management fee (#142) is attributed to its
  owner(s) and priced as cash + a bracket-aware deduction.

Absence is a strict no-op throughout: an account declaring none of these
leaves its pot on today's global rate, fully liquid and fee-free (DP#32).
"""
from __future__ import annotations

from typing import Any, Dict, List

from contract_errors import ContractAdaptationError
from contract_people import (
    _active_employment_income, _find_primary_and_spouse, _horizon_end_year,
    _owner_shares, _people_by_id,
)


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
    'mer_drag': {kind: {mer_balance, weighted_mer_sum}},
    'mgmt_fees': {person_id: annual_fee}}``.

    - ``mer_drag[kind]`` (issue #691/#136): the summed balance of accounts of
      ``kind`` that declared a `mer` fee, the balance-weighted fee sum
      (``sum(balance * mer)``), and -- issue #136 -- the fee as a FIXED DRAG
      RATE (``fee_rate``, the balance-weighted average fee; ``fee_share``, the
      fee-accounts' share of the kind's DECLARED pot balance). The growth rule
      subtracts ``fee_share * fee_rate`` from the gross rate. The fee is a
      RATE, not a frozen dollar snapshot, so it does not dilute as the pot
      grows; a zero-opening-balance fee account is treated as the whole pot
      when it is the only declared account of its kind (it must still pay its
      declared fee once funded, issue #136). A null/absent `mer` records
      nothing (fee-free global rate, golden); an explicit 0.0 is recorded (a
      declared fact, DP#32) but moves no rate.

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
    # Issue #142: {person_id: annual s.20(1)(e)-deductible management fee},
    # attributed pro rata to the account's owner(s) (a joint non-reg account
    # splits its fee by declared ownership shares, the same split every
    # other owner-attributed fact uses). Collected by the kind gate below.
    mgmt_fees: Dict[str, float] = {}
    primary_id, spouse_id = _find_primary_and_spouse(doc)
    couple = {primary_id, spouse_id} - {None}
    for acc in doc.get("accounts", []):
        fee = acc.get("deductible_management_fee_annual")
        if fee is None:
            continue
        if acc["kind"] != "non_reg":
            raise ContractAdaptationError(
                f"Account {acc['id']!r} (kind={acc['kind']}) declares "
                f"deductible_management_fee_annual={fee!r}. ITA s.20(1)(e) "
                f"allows a deduction only for fees paid to manage or "
                f"administer NON-REGISTERED investments -- a fee inside a "
                f"registered plan is not deductible and this field is not "
                f"its spelling. Drop the field here (issue #142)."
            )
        owners = _owner_shares(acc.get("owner"))
        if not set(owners) <= couple:
            # A fee on an ADDITIONAL ACCUMULATING adult's account has no tax
            # seam to reach (#899's adults never retire and their prologue
            # tax path carries no deductions) -- silently dropping it would
            # price a phantom deduction. Refuse rather than drop (DP#32).
            raise ContractAdaptationError(
                f"Account {acc['id']!r} declares "
                f"deductible_management_fee_annual={fee!r} but is owned by "
                f"{sorted(set(owners) - couple)!r}, outside the simulated "
                f"couple. An additional accumulating adult's fee has no tax "
                f"path to reach yet (#899/#901) -- move the account to the "
                f"primary or spouse, or drop the field (issue #142)."
            )
        for person_id, share in owners.items():
            mgmt_fees[person_id] = (mgmt_fees.get(person_id, 0.0)
                                    + float(fee) * share)
    # Issue #136: the pot-kind's DECLARED total balance (sum of every account
    # of this kind, fee-bearing or not) -- used to price the fee-accounts' share
    # of the pot. A zero-opening fee account funded by future contributions
    # keeps its fee live because the DRAG RATE (not a rolling dollar sum) is
    # what the growth rule subtracts.
    kind_declared: Dict[str, float] = {}
    fee_rates_by_kind: Dict[str, List[float]] = {}
    for acc in doc.get("accounts", []):
        kind = acc["kind"]
        if kind not in _GROWTH_POT_KINDS:
            continue
        amount = acc["balance"]["amount"]
        kind_declared[kind] = kind_declared.get(kind, 0.0) + amount
        mer = acc.get("mer")
        if mer is not None and mer != 0:
            fee_rates_by_kind.setdefault(kind, []).append(mer)
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
        # Issue #691: a per-account MER (management-expense-ratio) fee, summed
        # balance-weighted into its pot so the growth rule subtracts it from the
        # gross rate (net = gross - sum(balance*mer)/pot_total). DP#32: an
        # explicit 0.0 IS a declared fact (fee-free), recorded here (mer_balance
        # counted, weighted_mer_sum contributes 0) and distinct from a null/
        # absent MER, which records nothing and leaves today's global-rate
        # behaviour untouched (the canonical fee (DP#8)).
        mer = acc.get("mer")
        if mer is not None:
            m = mer_drag.setdefault(kind, {"mer_balance": 0.0,
                                           "weighted_mer_sum": 0.0})
            m["mer_balance"] += amount
            m["weighted_mer_sum"] += amount * mer
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
    # Issue #136: normalize each pot-kind's fee entry to a FIXED DRAG RATE ---
    # ``fee_rate`` (the balance-weighted average declared fee over the kind's
    # fee-flagged accounts, or the declared average when they all open
    # zero-balance) and ``fee_share`` (the fee-accounts' share of the kind's
    # DECLARED pot balance). The growth rule subtracts ``fee_share * fee_rate``
    # -- a constant rate every year, immune to the pot's dollar drift (no
    # dilution as an ongoing plan, and a zero-balance account that is the only
    # account of its kind gets the WHOLE pot fraction once funded -- issue
    # #136).
    for kind, entry in mer_drag.items():
        bal = entry["mer_balance"]
        wms = entry["weighted_mer_sum"]
        rates = fee_rates_by_kind.get(kind, [])
        if bal > 0:
            entry["fee_rate"] = wms / bal
        elif rates:
            # All fee-flagged accounts of this kind close zero-balance (e.g. a
            # brand-new account being funded later): keep the DECLARED average
            # fee rate -- a single 0-opening account at 0.5% is a 0.5% pot once
            # it holds money (issue #136).
            entry["fee_rate"] = sum(rates) / len(rates)
        else:
            entry["fee_rate"] = 0.0
        total = kind_declared.get(kind, 0.0)
        if total > 0:
            entry["fee_share"] = bal / total
        else:
            # No other declared account of this kind: the fee-flagged account
            # (or the future money in this pot) IS the whole pot.
            entry["fee_share"] = 1.0 if (bal > 0 or rates) else 0.0
    return {"return_overrides": return_overrides, "locked": locked,
            "mer_drag": mer_drag, "mgmt_fees": mgmt_fees}


def map_management_fee_legs(doc: Dict, start_year: int) -> List[Dict[str, Any]]:
    """Issue #142: every declared non-registered
    ``deductible_management_fee_annual`` becomes dated NEGATIVE cash-flow legs
    -- one per projection year, folded into the engine's EXISTING dated
    cash-flow channel by ``input_contract.to_internal_config`` (the same
    channel #138's insurance premiums and #139's transaction costs ride), so
    the fee is REAL CASH paid to the manager, never a phantom deduction.

    The deduction itself does NOT live on the legs: it is priced in the tax
    fold (bracket-aware taxable-income reduction while the owner works; OAS-
    clawback-base reduction once they are retired). The legs carry
    ``tax_treatment: post-tax`` because the leg is after-tax cash -- the
    saving is booked where the tax is computed, not by re-pricing the leg.

    A discretionary mandate charges while the account exists, so a PERPETUAL
    fee (no end date in the contract) prices through the horizon person's
    final simulated year. Returns ``[]`` for a household declaring no fee --
    the golden household -- leaving the fold byte-identical (DP#32).
    """
    fee_accounts = [a for a in doc.get("accounts", [])
                    if a.get("deductible_management_fee_annual") is not None]
    if not fee_accounts:
        return []
    primary_id, _ = _find_primary_and_spouse(doc)
    last_year = (_horizon_end_year(doc, primary_id) if primary_id else None)
    if last_year is None:
        # Same generous cap map_insurance_premiums uses when the horizon does
        # not date against the primary: extra legs beyond the fold's own span
        # are inert by construction.
        last_year = start_year + 99
    out: List[Dict[str, Any]] = []
    for acc in fee_accounts:
        amount = -float(acc["deductible_management_fee_annual"])
        for year in range(start_year, last_year + 1):
            out.append({
                "year": year,
                "amount": amount,
                "tax_treatment": "post-tax",
                "kind": "cost",
                "id": acc["id"],
                "label": "non-registered management fee",
            })
    return out


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
