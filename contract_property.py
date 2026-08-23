"""The NON-principal ``properties[]``: the cottage, the rental, the plex.

``_map_owned_properties`` carries every property the couple owns onto the
annual balance sheet as a ``{id, kind, net_equity}`` entry (issue #692), plus
whatever dynamics the document declares on it: appreciation (#956 bite A), a
dated purchase with financing (#696/#967), a dated sale (#956 bites B/C),
recurring carrying costs (#1010), a funding sweep (#1011), and rental income
with CCA and short-term-rental classification (#693/#694/#697).

The PRINCIPAL residence is deliberately excluded here -- its value reaches the
annual side through ``cfg['property']`` (LTV and charge math), so counting it
again in ``cfg['properties']`` would double it. Its mapping lives in
``contract_principal``.

``_annual_amortization_schedule`` is the ONE spelling of a financed purchase's
amortization (DP#9): the servicing rule, the rental interest deduction and the
balance-sheet debt total all read the schedule it builds, never three
computations that could drift.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from contract_people import _owner_shares


def _find_property(doc: Dict, kind: str) -> Optional[Dict]:
    for prop in doc.get("properties", []):
        if prop["kind"] == kind:
            return prop
    return None


def _sale_calendar_year(sale: Dict) -> int:
    """The calendar year a property's ``sale`` fires (issue #956 bite C).

    Reads the sweepable numeric ``year`` leaf when present, and derives it from
    ``date`` otherwise (``int(date[:4])``). An explicit ``is not None`` test --
    never ``sale.get("year") or int(...)`` (DP#32: a year of 0 is a real value
    the schema permits, and ``or`` would make it unrepresentable). Exactly one
    of ``year``/``date`` is guaranteed present by the schema's ``property_sale``
    oneOf. Mirrors the spelling `_map_property_sale` / `_map_principal_sale`
    already use to carry the sale year onto the config.
    """
    return (sale["year"] if sale.get("year") is not None
            else int(sale["date"][:4]))


def _property_sold_by_terminal(prop: Dict, terminal_year: Optional[int]) -> bool:
    """Issue #964: is this property SOLD on/before the terminal (death) year?

    A property whose ``sale`` fires on/before the terminal year is NOT owned at
    death -- the disposition rule already converted it to portfolio cash (the
    reinvested proceeds), so the estate must NOT value it again at its death-year
    deemed disposition (that is the double-count #964 is about). A ``sale.year``
    beyond the terminal year never fires inside the projection -> the property IS
    still owned at death -> keep it in the estate. ``sale`` absent -> held to the
    horizon -> keep it. ``terminal_year`` None (the horizon does not date
    against the primary -- an unreachable estate path in practice, but defended
    here) -> conservatively keep the property (do not drop a value on an unknown
    terminal year -- DP#32: absence must not silently zero a real asset).
    """
    sale = prop.get("sale")
    if sale is None or terminal_year is None:
        return False
    return _sale_calendar_year(sale) <= terminal_year


def _annual_amortization_schedule(principal: float, annual_rate: float,
                                 amortization_years: float,
                                 origination_year: int,
                                 projection_years: int) -> List[Dict[str, float]]:
    """Issue #967: a mid-horizon mortgage's ANNUAL amortization schedule,
    one entry per calendar year from ``origination_year`` to the payoff year
    (or the projection horizon, whichever comes first).

    A pure function (DP#3) of the originated principal, the declared rate,
    the amortization term, and the origination year -- it reads no simulation
    state, so the schedule the servicing rule amortizes against and the
    interest the rental deduction claims are ONE spelling (DP#9), never two
    computations that could drift. It reuses the standard annuity formula
    (``countries.canada.rate_model.monthly_payment`` -- the SAME function the
    year-0 principal mortgage's schedule is built from), so this does NOT
    write a new amortization engine; it builds a per-property annual slice
    from the existing one.

    Each entry: ``{year, opening_balance, interest, principal, payment,
    end_balance}``. ``interest`` is ``opening_balance * annual_rate`` (the
    annual interest charged on the outstanding balance -- the figure the
    rental deduction claims under s.20(1)(c)). ``principal`` is
    ``payment - interest``. In the payoff year the remaining balance is
    closed exactly (``payment = opening_balance + interest``) so no residual
    lingers past the term, mirroring ``apply_consumer_loans``'s final-year
    close. A 0% rate amortizes straight-line (principal / term), the same
    degenerate path ``monthly_payment`` takes. The schedule stops at the
    payoff year (``end_balance <= 0``); entries past it are NOT emitted, so a
    mortgage that pays off before the horizon contributes nothing after.
    """
    from countries.canada.rate_model import monthly_payment
    schedule: List[Dict[str, float]] = []
    if principal <= 0 or amortization_years <= 0:
        return schedule
    monthly = monthly_payment(principal, annual_rate, amortization_years)
    annual_payment = monthly * 12.0
    balance = principal
    # A mortgage cannot outlive its amortization term: count the payoff year
    # from the term (the same term-starts-at-origination convention
    # apply_consumer_loans uses), capped at the projection horizon so a long
    # term does not build entries past the run.
    term_years = int(amortization_years) if amortization_years == int(amortization_years) else None
    for i in range(projection_years):
        year = origination_year + i
        if balance <= 1e-9:
            break
        interest = balance * annual_rate
        # The final year of the declared term: close the loan exactly.
        if term_years is not None and i + 1 >= term_years:
            payment = balance + interest
        else:
            payment = min(annual_payment, balance + interest)
        principal_paid = payment - interest
        end_balance = max(0.0, balance - principal_paid)
        schedule.append({
            'year': year,
            'opening_balance': balance,
            'interest': interest,
            'principal': principal_paid,
            'payment': payment,
            'end_balance': end_balance,
        })
        balance = end_balance
    return schedule


def _map_owned_properties(doc: Dict, primary_id: str,
                          spouse_id: Optional[str],
                          projection_years: int = 0,
                          family_pre_window: Optional[int] = None) -> List[Dict[str, Any]]:
    """Every NON-principal real property the couple owns, as a first-class
    ``{id, kind, net_equity}`` list for the ANNUAL balance sheet (issue #692,
    epic #690 bite 1).

    Before this seam the annual side (``prop_cfg`` -> ``house_value``) read
    exactly ONE ``kind="principal"`` residence; a declared cottage or rental
    the couple also owned was dropped from ``SimState`` and so from every
    annual metric (net worth / ``total_assets``), surfacing only at the
    terminal estate (``_map_estate`` already values + taxes it there). This
    carries each such property forward so the household's stated real estate
    is on the annual balance sheet, not silently truncated to the first match
    (DP#32 -- absence must be explicit, not a favourable understatement).

    The principal residence is EXCLUDED here on purpose: its value already
    reaches the annual side via ``prop_cfg`` (LTV / charge math) and is not
    what #692 reports dropped. Counting it here too would double it.

    ``net_equity`` is the couple's share of ``value - (mortgage/heloc secured
    against THIS property)``. It is a STATIC balance-sheet figure this bite:
    rental income (#693), CCA (#694), the per-year PRE allocation (#695), a
    mid-horizon purchase (#696) and STR (#697) are later bites that give the
    property dynamics -- deliberately not modelled here.
    """
    couple = [pid for pid in (primary_id, spouse_id) if pid is not None]
    principal = _find_property(doc, "principal")
    owned: List[Dict[str, Any]] = []
    for prop in doc.get("properties", []):
        if principal is not None and prop["id"] == principal["id"]:
            continue
        shares = _owner_shares(prop["owner"])
        couple_share = sum(frac for pid, frac in shares.items() if pid in couple)
        if couple_share <= 0:
            continue  # someone else's property (e.g. the grandparents' cottage)
        # A mortgage/HELOC whose collateral is THIS property reduces its
        # equity. Such a facility is not the principal residence's charge, so
        # it never reached mortgage_balance/heloc_balance (those are looked up
        # by the principal's id) -- netting it here is the only place this
        # property's financing is accounted for, so there is no double count.
        secured_liabs = [
            liab for liab in doc.get("liabilities", [])
            if liab["kind"] in ("mortgage", "heloc")
            and liab.get("collateral") == prop["id"]
        ]
        secured = sum(liab["balance"]["amount"] for liab in secured_liabs)
        # Issue #967: a MORTGAGE originated at a mid-horizon purchase
        # (purchase.financing) is a secured liability against THIS property
        # that does NOT exist at year 0 (it originates in the purchase year),
        # so it is not among the year-0 `secured_liabs` above. It reduces the
        # property's equity the SAME way a year-0 secured mortgage does --
        # the down payment (value - mortgage_amount) is what the household
        # funds, not the full value -- so its principal joins `secured` here
        # for the net_equity / secured_share computation. `financing` is read
        # ONLY off a declared `purchase` (the schema requires purchase to
        # carry financing), and only when present, so a property with no
        # financing round-trips byte-identical to #696 (DP#32). The principal
        # is taken at the couple's share, mirroring liability.balance above.
        purchase = prop.get("purchase")
        financing = (purchase.get("financing")
                     if purchase is not None else None)
        # Issue #1011: `funding_options` is the SWEEP form of `financing` --
        # the optimizer enumerates and ranks candidate funding methods for this
        # purchase rather than funding it one fixed way. The schema makes
        # `financing` and `funding_options` mutually exclusive (a purchase is
        # either funded one fixed way or swept, never both), so when
        # `funding_options` is declared `financing` is None. Read here ONLY to
        # carry it onto the annual-side entry for the discovery/exploration to
        # enumerate; it is NOT a financing input itself (the engine never
        # services a `funding_options` list -- it services the `financing`
        # block the overlay materializes per chosen option). Absent => no
        # funding sweep, byte-identical to #967/#696 (DP#32).
        funding_options = (purchase.get("funding_options")
                           if purchase is not None else None)
        financed_principal = 0.0
        if financing is not None:
            financed_principal = financing["mortgage_amount"]
            secured += financed_principal
        net_equity = (prop["value"]["amount"] - secured) * couple_share
        entry: Dict[str, Any] = {
            "id": prop["id"],
            "kind": prop["kind"],
            "net_equity": net_equity,
        }
        # Issue #956 bite A: a declared real annual appreciation rate lets the
        # property's GROSS value compound year over year (equity = appreciated
        # value - the static secured mortgage), so a purchase-timing sweep sees
        # the dominant driver of real-estate timing. Carry the couple's share of
        # gross value and secured mortgage SEPARATELY (net_equity alone cannot be
        # appreciated -- the mortgage portion must not grow). Emitted only when
        # appreciation_rate is declared, so a property with no rate round-trips
        # byte-identically to #692/#696 (DP#32). The rate is a sweepable numeric
        # leaf (sensitivity.sweeps) so the optimizer can range the assumption.
        #
        # Issue #956 bite B: a dated mid-horizon SALE (prop["sale"]) needs the
        # SAME couple-share gross value, secured mortgage, and adjusted cost
        # base to price its disposition gain (value_share - acb_share is the
        # gross gain; the secured_share is the debt retired at sale). The tax
        # layer built on top of this bite consumes all three. So the shares are
        # carried whenever appreciation_rate OR sale is declared; a property
        # with neither still round-trips byte-identically to #692/#696 (DP#32).
        #
        # `acb_share`: the couple's share of the adjusted cost base. The schema
        # declares `property.acb` as `money | null` (a bare number, not an
        # {amount} object -- read directly, mirroring `_map_pre_property_gains`
        # which does `prop["acb"] * share`). A null ACB is not unknown-to-zero;
        # it means "no accrued gain yet" -- a property bought at value has
        # ACB == value -- so the fallback is `value_share`, never 0.0 (DP#32:
        # zero is a value, not a fallback).
        appreciation_rate = prop.get("appreciation_rate")
        sale = prop.get("sale")
        # Issue #967: a financed purchase also needs the couple-share gross
        # value and secured mortgage carried SEPARATELY (net_equity alone
        # cannot be amortized -- the financed mortgage portion is a serviced
        # liability that amortizes down, so the equity = appreciated value -
        # the (declining) mortgage must read value_share / secured_share, not
        # a static net_equity). So the shares are carried when
        # appreciation_rate OR sale OR financing is declared; a property with
        # none of the three still round-trips byte-identical to #692/#696
        # (DP#32).
        #
        # Issue #1010: a carrying_costs block declaring a `fraction_of_value`
        # ALSO needs the couple-share gross value carried, because the fraction
        # is applied to the property's CURRENT (appreciating) gross value --
        # `net_equity` alone cannot be fractioned (it is net of the secured
        # mortgage, and property tax is assessed on gross value, not equity).
        # So the gate fires for a non-null `fraction_of_value` too. A
        # carrying_costs block with only a flat `annual_amount` (no fraction)
        # does NOT need value_share and stays out of this block, byte-identical
        # to a household with no appreciation/sale/financing (DP#32).
        #
        # Issue #1011: a property whose purchase declares `funding_options`
        # ALSO needs these shares carried, because the funding sweep rebuilds
        # `net_equity`/`secured_share` PER candidate funding method (an
        # all-cash option draws the full value; a mortgage option draws only
        # the down payment). Without `value_share`/`secured_share` the overlay
        # could not recompute the down payment for a candidate when the
        # property has no `financing`/`appreciation_rate`/`sale` of its own --
        # so the carry condition is extended to include `has_funding_options`.
        has_financing = financing is not None
        carrying_costs = prop.get("carrying_costs")
        cc_fraction = (carrying_costs.get("fraction_of_value")
                       if carrying_costs is not None else None)
        has_funding_options = funding_options is not None
        if (appreciation_rate is not None or sale is not None or has_financing
                or cc_fraction is not None or has_funding_options):
            value_share = prop["value"]["amount"] * couple_share
            entry["value_share"] = value_share
            entry["secured_share"] = secured * couple_share
            acb = prop.get("acb")
            if acb is not None:
                entry["acb_share"] = acb * couple_share
            else:
                # null ACB = no accrued gain yet (bought at value): the gain
                # inputs collapse to value_share - value_share == 0, the correct
                # disposition gain for a just-acquired property (DP#32).
                entry["acb_share"] = value_share
            if appreciation_rate is not None:
                entry["appreciation_rate"] = appreciation_rate
        # Issue #1010 (epic #956): a property's RECURRING carrying costs
        # (property tax, maintenance, insurance) charged each year over the
        # ownership window through the solvency waterfall. The flat
        # `annual_amount` is taken at the couple's ownership share (mirroring
        # `purchase.closing_costs` and `sale.selling_costs`); `fraction_of_value`
        # is carried RAW and applied to the couple-share gross value in the fold
        # (value_share already embeds the couple share, so the fraction needs no
        # extra scaling). Both components are read with explicit `is not None`
        # tests -- never `cc.get(k) or 0` (DP#32: a real $0 / 0.0 is a legitimate
        # zero, not an unknown). Emitted ONLY when `carrying_costs` is declared
        # and at least one component is non-null, so an absent block (and a
        # declared `{}`) round-trips byte-identical to today -- the property is
        # free to carry, exactly as before (DP#32). Forbidden on the principal
        # residence by the schema (its costs live in annual_living_costs), so a
        # non-null block never reaches here for kind=principal.
        if carrying_costs is not None:
            cc_amount = carrying_costs.get("annual_amount")
            cc_entry: Dict[str, Any] = {}
            if cc_amount is not None:
                cc_entry["annual_amount"] = cc_amount * couple_share
            if cc_fraction is not None:
                cc_entry["fraction_of_value"] = cc_fraction
            if cc_entry:
                entry["carrying_costs"] = cc_entry
        # Issue #696 (epic #690 bite 5): a dated MID-HORIZON PURCHASE. When
        # declared, the property is BOUGHT on `purchase.date` rather than held
        # from year 0: it contributes no equity/rent/CCA before that calendar
        # year, and in the purchase year the household funds the down payment
        # (`net_equity` -- value less the mortgage originated against it, at the
        # couple's share) plus the couple's share of `closing_costs` out of
        # cash (simulation._prologue / _property_equity_for_year). The calendar
        # year is derived from the date exactly as a cash_flow's is
        # (int(date[:4]), see the cash_flows map). Emitted only when `purchase`
        # is present, so every property held from year 0 round-trips
        # byte-identically to #692 (DP#32).
        purchase = prop.get("purchase")
        if purchase is not None:
            # Issue #956 bite C: the calendar year is read from a declared
            # numeric `year` leaf when present (a sweepable alternative to
            # `date`, ranged via sensitivity.sweeps), and derived from `date`
            # otherwise (int(date[:4]), the same spelling a cash_flow uses). An
            # explicit `is not None` test -- never `purchase.get("year") or
            # int(...)` (DP#32: `0` would be unrepresentable, and a year of 0
            # is a real value the schema permits). Exactly one of `year`/`date`
            # is guaranteed present by the schema's oneOf.
            pyear = (purchase["year"] if purchase.get("year") is not None
                     else int(purchase["date"][:4]))
            entry["purchase"] = {
                "year": pyear,
                "closing_costs": purchase["closing_costs"] * couple_share,
            }
            # Issue #967: a MORTGAGE originated against this property at the
            # purchase year. When `purchase.financing` is declared, the
            # property is equity-financed only for the DOWN PAYMENT (value -
            # mortgage_amount, couple share -- already `net_equity` above);
            # the mortgage funds the rest, originates as a serviced secured
            # liability in `pyear`, and its interest is deductible when the
            # property is a rental (ITA s.20(1)(c)) and NON-deductible for a
            # recreational/personal property. The deductibility follows the
            # property's `kind` -- a cottage (kind=recreational) has no
            # `rental` block, so its mortgage interest never reaches the
            # rental deduction fold, by construction. Carried on the entry's
            # `purchase` block (mirroring how the year/closing_costs ride
            # there) so the fold's servicing rule can find it. The principal
            # is taken at the couple's share, mirroring liability.balance and
            # the `secured` netting above. Emitted only when `financing` is
            # present, so an equity-financed purchase round-trips byte-
            # identical to #696 (DP#32). `owner_roles` is the couple's
            # owner->role split (mirroring the rental/sale mapping) so the
            # servicing + deduction can apportion to each taxed member.
            if financing is not None:
                owner_roles = {
                    role: shares.get(pid, 0.0)
                    for role, pid in (("primary", primary_id),
                                      ("spouse", spouse_id))
                    if pid is not None and shares.get(pid, 0.0) > 0
                }
                financed_principal_share = financing["mortgage_amount"] * couple_share
                # Issue #967: precompute the mortgage's ANNUAL amortization
                # schedule from origination to payoff (or the horizon), so the
                # servicing rule, the rental interest deduction, and the
                # balance-sheet total_debt all read ONE schedule (DP#9 -- one
                # spelling, never three computations that could drift). The
                # schedule covers `projection_years` from `pyear`; a mortgage
                # whose term outlives the horizon is capped at the horizon (the
                # fold never reaches past it, so an unserviced trailing slice
                # would be dead data). Built by the pure `_annual_amortization_
                # schedule` helper, which reuses the standard annuity formula
                # (monthly_payment) -- NOT a new amortization engine.
                schedule = _annual_amortization_schedule(
                    financed_principal_share, financing["rate"],
                    financing["amortization_years"], pyear,
                    projection_years)
                entry["purchase"]["financing"] = {
                    "mortgage_amount": financed_principal_share,
                    "rate": financing["rate"],
                    "rate_type": financing["rate_type"],
                    "amortization_years": financing["amortization_years"],
                    "origination_year": pyear,
                    "deductible": prop["kind"] == "rental",
                    "owner_roles": owner_roles,
                    "schedule": schedule,
                }
            # Issue #1011: carry the declared `funding_options` (the SWEEP form
            # of `financing`) onto the annual-side purchase block, plus the
            # `funding_recompute` inputs the per-option overlay needs to
            # materialize a `financing` block for any chosen candidate WITHOUT
            # re-deriving the owner structure / kind / horizon here a second
            # time. `value_share` (carried above because has_funding_options
            # extends the carry condition) plus `secured_base` let the overlay
            # recompute net_equity/secured_share per option; `owner_roles`/
            # `deductible`/`projection_years` let it rebuild the financing
            # block identically to how the fixed-`financing` branch above
            # builds it (DP#9: the schedule itself comes from the SAME pure
            # `_annual_amortization_schedule` helper, never a second spelling).
            # `secured_base` is the couple-share of the NON-financing secured
            # debt (year-0 mortgages/HELOCs collateralized on this property),
            # invariant across funding candidates. Emitted only when
            # `funding_options` is declared, so a property with neither
            # `financing` nor `funding_options` round-trips byte-identical to
            # #967/#696 (DP#32). The engine never reads `funding_options` or
            # `funding_recompute` itself -- they are sweep metadata consumed
            # only by scenario_discovery / optimize.run_property_funding_
            # exploration (DP#18: a write that reaches a real reader).
            if funding_options is not None:
                owner_roles = {
                    role: shares.get(pid, 0.0)
                    for role, pid in (("primary", primary_id),
                                      ("spouse", spouse_id))
                    if pid is not None and shares.get(pid, 0.0) > 0
                }
                entry["purchase"]["funding_options"] = [
                    dict(opt) for opt in funding_options]
                entry["purchase"]["funding_recompute"] = {
                    "secured_base": (secured - financed_principal) * couple_share,
                    "owner_roles": owner_roles,
                    "deductible": prop["kind"] == "rental",
                    "projection_years": projection_years,
                }
        # Issue #956 bite B: a dated mid-horizon voluntary SALE -- the
        # symmetric inverse of the #696 purchase above. When declared, the
        # property LEAVES the balance sheet in the calendar year of `sale.date`
        # (its equity gates to zero from that year on, see
        # `_property_equity_for_year`), and the tax + proceeds layer built on
        # top of this bite invests the net proceeds (value_share less
        # secured_share, selling_costs, and disposition tax) into the
        # portfolio. The calendar year is derived from the date exactly as a
        # cash_flow's is (int(date[:4]), mirroring the purchase mapping).
        # `selling_costs` is read explicitly -- never `sc or 0.0` (DP#32 forbids
        # `x or DEFAULT`): a null `selling_costs` is a real $0 in disposition
        # costs, distinct from a sale that has not been priced. The schema
        # declares it `money | null` (a bare number, the same shape as
        # `closing_costs` above), so it is read directly, not as an {amount}
        # object. Emitted only when `sale` is present, so a property held to the
        # horizon round-trips byte-identical to #692/#696 (DP#32).
        #
        # The tax + proceeds layer (this bite's sale-core) prices the
        # disposition gain per OWNER: Canada has no joint filing, so each
        # spouse reports their share of the gain on their OWN return and it
        # bands against THAT spouse's taxable income (the same per-owner split
        # the rental `owner_roles` field below drives for net rental income).
        # The gain inputs above (value_share / secured_share / acb_share) are
        # the COUPLE'S aggregate share; the per-owner split is carried here as
        # `owner_roles` (mirroring the rental mapping's spelling exactly) so
        # the disposition rule can apportion the gain to each owner without
        # re-deriving the owner structure from the contract (the mapped entry
        # carries no `owner` field). Each role's fraction is its declared %
        # of the property (a couple owning a cottage 50/50 -> {'primary': 0.5,
        # 'spouse': 0.5}); the fractions sum to couple_share, so weighting the
        # couple-share gain by role_frac/couple_share recovers each owner's
        # share of the gain. Emitted only when `sale` is present, so a property
        # held to the horizon round-trips byte-identically to #692/#696 (DP#32).
        if sale is not None:
            sc = sale.get("selling_costs")
            selling_costs = (sc if sc is not None else 0.0) * couple_share
            # Issue #956 bite C: the sale calendar year is read from a declared
            # numeric `year` leaf when present (a sweepable alternative to
            # `date`), and derived from `date` otherwise (int(date[:4]),
            # mirroring the purchase mapping above). An explicit `is not None`
            # test -- never `sale.get("year") or int(...)` (DP#32). Exactly one
            # of `year`/`date` is guaranteed present by the schema's oneOf.
            syear = (sale["year"] if sale.get("year") is not None
                     else int(sale["date"][:4]))
            owner_roles = {
                role: shares.get(pid, 0.0)
                for role, pid in (("primary", primary_id), ("spouse", spouse_id))
                if pid is not None and shares.get(pid, 0.0) > 0
            }
            # Issue #956 bite B (sale-core): carry the property's PRE designation
            # periods so the disposition rule can apportion the gain (ITA
            # s.40(2)(b)) -- a property designated as a principal residence for
            # some years shelters the gain apportioned to those years. The
            # periods are carried RAW (the same shape `designated_years` reads);
            # the rule resolves them to the designated-year SET capped at the
            # sale year (a property sold mid-horizon cannot designate years
            # after its sale). A property with no designation carries [] -- the
            # rule's `taxable_gain_fraction(0, ...) == 1.0` (fully taxable).
            entry["sale"] = {
                "year": syear,
                "selling_costs": selling_costs,
                "owner_roles": owner_roles,
                "designated_principal_residence_years":
                    prop.get("designated_principal_residence_years", []),
            }
            # Issue #969: carry the family-level PRE window onto the sale so
            # the disposition rule prices the gain against the FAMILY
            # denominator (ITA s.40(2)(b)), not this property's own span in
            # isolation (the single-property approximation that over-shelters
            # a second property's gain). Carried ONLY when a genuine family
            # contest exists (>=2 properties with designations); absent is the
            # byte-identical legacy sentinel -- a single-property household
            # falls back to its own window, unchanged (DP#32).
            if family_pre_window is not None:
                entry["sale"]["family_pre_window"] = family_pre_window
        # Issue #693 (epic #690 bite 2): a rental property produces NET RENTAL
        # INCOME (gross rent - operating expenses, ITA/CRA T776) taxable at the
        # owner's marginal rate, and the mortgage interest on it is DEDUCTIBLE
        # (ITA s.20(1)(c)) because the property earns income. Carry the declared
        # T776 facts, the annual interest on the debt secured against THIS
        # property (balance x rate -- the same facilities netted from equity
        # above), and the couple's OWNER->ROLE split so the fold can attribute
        # the income/deduction to each taxed member. `rental` is emitted only
        # for a kind=rental property that actually declares `rental` facts;
        # absent otherwise, so a cottage (kind=recreational) carries equity only
        # and every non-rental household round-trips byte-identically (DP#32).
        rental = prop.get("rental")
        if prop["kind"] == "rental" and rental is not None:
            mortgage_interest = sum(
                liab["balance"]["amount"] * liab["rate"] for liab in secured_liabs
            )
            owner_roles = {
                role: shares.get(pid, 0.0)
                for role, pid in (("primary", primary_id), ("spouse", spouse_id))
                if pid is not None and shares.get(pid, 0.0) > 0
            }
            entry["rental"] = {
                "gross_rent_annual": rental["gross_rent_annual"],
                "expenses_annual": rental["expenses_annual"],
                "mortgage_interest_annual": mortgage_interest,
                "owner_roles": owner_roles,
            }
            # Issue #967: a financed rental's mortgage interest is DEDUCTIBLE
            # under s.20(1)(c), but the mortgage ORIGINATES at the purchase
            # year -- so the static `mortgage_interest_annual` above (built
            # from year-0 secured_liabs) is $0 for a financed rental (the
            # mortgage did not exist at year 0). The per-year interest lives
            # on the financing block's precomputed schedule; carry a REFERENCE
            # to it on the rental block so `simulation._rental_income_for` can
            # add the dynamic interest for `sim_year` to the deduction. A
            # rental with no financing (held from year 0, or equity-financed
            # at purchase) has no `financing` here -> the rental fold reads
            # the static `mortgage_interest_annual` only, byte-identical to
            # #693 (DP#32). A cottage (kind=recreational) has no `rental`
            # block at all, so its financed interest never reaches the rental
            # deduction -- non-deductible by construction, as required.
            fin_ref = (entry.get("purchase", {}).get("financing")
                       if purchase is not None else None)
            if fin_ref is not None:
                entry["rental"]["financing_schedule"] = fin_ref["schedule"]
            # Issue #694 (epic #690 bite 3): an optional Capital Cost Allowance
            # election. When declared, the fold depreciates the building against
            # rental income each year (lowering taxable income, tracking the
            # UCC) and RECAPTURES the previously-claimed CCA as ordinary income
            # at the estate's deemed disposition (ITA s.13(1)). Carried through
            # as declared -- a rental the couple owns whole, so the capital
            # cost / UCC are the couple's depreciable figures; `fmv_at_disposition`
            # is the couple's disposition proceeds (value x couple_share, the SAME
            # figure `_map_estate` taxes the capital gain on), so the recapture
            # ceiling and the estate gain share one property valuation. Emitted
            # only when `cca` is present, so a rental without a CCA election
            # round-trips byte-identically to #693 (DP#32).
            cca = rental.get("cca")
            if cca is not None:
                entry["rental"]["cca"] = {
                    "rate": cca["rate"],
                    "capital_cost": cca["capital_cost"],
                    "opening_ucc": cca["opening_ucc"],
                    "fmv_at_disposition": prop["value"]["amount"] * couple_share,
                }
            # Issue #697 (epic #690 bite 6): a SHORT-TERM rental (Airbnb-style)
            # is a legally and fiscally different animal. When `short_term` is
            # declared, the Canada jurisdiction module rules on its LEGALITY
            # (countries/canada/short_term_rental) and REFUSES to map any income
            # for an STR in a banned borough or without a CITQ registration --
            # a loud refusal at load, never a silent "assume legal" (DP#25: the
            # rule lives in the jurisdiction module, this adapter only invokes
            # it; DP#32: absence of confirmed legality fails, it is not guessed).
            # A PERMITTED STR carries its business-income classification (ITA s.9,
            # not passive T776 property income) and the GST/HST small-supplier
            # flag ($30k, ETA s.148) so the fold can surface them; its net income
            # still rides the SAME s.20(1)(c)/T776 net-income fold as a long-term
            # rental (business income is ordinary income too, DP#9). Emitted only
            # when `short_term` is present, so a plain rental round-trips
            # byte-identically to #693 (DP#32).
            short_term = rental.get("short_term")
            if short_term is not None:
                from countries.canada.short_term_rental import (
                    classify_str_income, require_str_permitted)
                require_str_permitted(
                    short_term["jurisdiction"], short_term["citq_registered"])
                effect = classify_str_income(
                    rental["gross_rent_annual"], rental["expenses_annual"],
                    mortgage_interest)
                entry["rental"]["short_term"] = {
                    "jurisdiction": short_term["jurisdiction"],
                    "citq_registered": short_term["citq_registered"],
                    "income_type": effect.income_type,
                    "gst_hst_registration_required":
                        effect.gst_hst_registration_required,
                }
        owned.append(entry)
    return owned
