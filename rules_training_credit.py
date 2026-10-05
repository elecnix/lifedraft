"""The ``training_credit`` rule: the Canada Training Credit (ITA s.122.91,
issue #372) -- a REFUNDABLE federal credit for an adult learner's tuition,
limited by a personal balance that builds $250 a year.

One module per government program (DP#10). The statute's arithmetic lives in
``countries.canada.training_credit`` (the law is Canadian, so it belongs in the
jurisdiction package, not in this generic rule module -- DP#25); this file is
the plumbing: read each taxed member's declared facts off the config, carry the
per-member balance, and write the outputs later rules and the year result read.

Why the rule is REFUNDABLE-shaped and the tuition rule is not
------------------------------------------------------------
The s.118.5 tuition credit is NON-refundable: it reduces tax, floors at zero,
and the unused remainder carries forward. The CTC is the opposite -- CRA
describes it as "If the amount of the training credit allowable exceeds tax
otherwise payable, the balance will be refunded" -- so it arrives as CASH in the
household's hand. It is therefore added to ``available`` by ``solvency`` (the
same seam a tax saving uses), not subtracted from anybody's tax. Getting this
backwards is the single most expensive mistake available here: a learner with no
tax to pay would see the credit vanish.

And it is not additive with the tuition credit: ITA s.122.91(3) requires the
eligible tuition going into the s.118.5 credit to be reduced by the CTC claimed
("Eligible tuition fees for the Tuition Tax Credit will be reduced by the
amount of the training tax credit deducted"). So this rule must run BEFORE
``tuition_credit``, which reads what is written here.

Only TAXED members are considered -- the credit is claimed by an individual on
their own return, and a child (who has no return of their own, #701) is out of
scope for it entirely. The two-slot primary/spouse shape matches the
``tuition_credit`` rule it has to reduce the base of; a household with a third
accumulating adult is already outside that rule's reach, and modelling a CTC
for an adult whose s.118.5 base is not computed would credit tuition against
nothing.
"""

from __future__ import annotations

from tax_data import default_tax_provider

from rule_registry import RuleContext, YearWorkingState, rule


def _is_usable_birth_year(value) -> bool:
    """Whether a member's ``birth_year`` is usable as an age.

    The credit is age-gated, so an unusable birth year must REFUSE -- and it
    must refuse with the MESSAGE that says why, not with a bare ``TypeError``
    or ``ValueError`` from arithmetic further down (Cite round 2).

    An ``int`` is the only accepted form, because that is what every other age
    computation in the engine expects (``is_retired``, the drawdown sizing --
    they all subtract it from a calendar year) and accepting a quoted year here
    would move the refusal to a stranger's arithmetic instead. A ``bool`` is
    rejected explicitly: it is an ``int`` subclass, and ``True`` is not a year.
    A negative or absurd year is NOT rejected: it is a fact the engine does not
    police elsewhere, and the age gate simply denies the credit for it.
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, int)


def _taxed_slots(config):
    """The members this rule considers: the two TAXED slots, primary then
    spouse, each read off the config the same way ``rules_tuition_credit``
    reads them so the two rules agree on WHICH person a number belongs to
    (DP#9). A member with no ``role`` is skipped rather than defaulted into the
    primary slot -- an absent role is an absent fact, not a primary.
    """
    members = config.family_members
    primary = next((m for m in members if m.get('role') == 'primary'), None)
    spouse = next((m for m in members if m.get('role') == 'spouse'), None)
    return [m for m in (primary, spouse) if m is not None]


@rule('training_credit')
def apply_training_credit(ws: YearWorkingState, ctx: RuleContext) -> bool:
    """Claim each taxed member's Canada Training Credit for ``sim_year`` and
    roll their training amount limit forward.

    For each of the two taxed members, in order:

    1. **The room.** ``ws.opening_training_amount_limit[member_id]`` -- the
       unused balance carried into the year, which already contains every $250
       conditioned on a PRIOR year's income -- falling back, while the member
       has no ledger entry, on their declared
       ``training_amount_limit_opening`` (their notice-of-assessment balance
       for the year before the projection). The fallback is per-member and
       applies only while there is no entry, so a spent or expired balance is
       never silently re-seeded from the declaration.
    2. **The claim.** The LESSER of the room, 50% of this year's ELIGIBLE
       tuition, and what is left of the $5,000 lifetime cap (the law, in
       ``countries.canada.training_credit.claimable_at``), gated on the member
       being 26 or older at the end of the year. "Eligible" already excludes
       employer-reimbursed fees -- ``contract_people._tuition_by_year`` nets
       them out -- so a learner's employer-paid course earns NO credit while
       still building their room, which is CRA's own worked example.
    3. **The accrual.** ``$250`` joins the balance for the NEXT year, and CRA
       conditions it on the income of the year it is earned in: at least CRA's
       indexed working-income threshold, and net income no higher than the top
       of the third federal bracket. CRA's own wording for the 2026 taxation
       year is "to accumulate the annual training amount limit of $250 for the
       2026 taxation year ... working income of $12,058 or more in 2025 ...
       net income for 2025 that did not exceed $177,882" -- the test is on the
       year whose income it is, and the room it creates is what the FOLLOWING
       year's claim draws on. So the rule adds this year's accrual after this
       year's claim, from this year's income. Reading a prior-year figure from
       the ledger here would bank the same $250 twice.
    4. **The expiry.** ITA s.122.91(4): an unused balance expires at the end of
       the year the member turns 65. The room is zeroed on that year, so the
       member keeps whatever they can still CLAIM in it (65 is within the claim
       window) and holds nothing afterwards.

    Outputs written to ``ws``: ``ctc_claimed_primary`` / ``ctc_claimed_spouse``
    (read by ``solvency`` as cash and by ``tuition_credit`` to reduce the
    s.118.5 base) and ``new_training_amount_limit`` (per member, carried into
    the next year).

    Absence-safe (DP#32): a household that declares no tuition and no opening
    balance claims nothing, accumulates nothing, and leaves every figure at its
    seeded default -- the golden fixture is a strict no-op, so the golden
    invariant cannot move. A member whose ``birth_year`` is missing while they
    DO declare tuition or an opening balance is REFUSED rather than priced: the
    credit is age-gated, so an unknown age is not a $0 credit, it is a missing
    input that would otherwise read as "under 26" and silently deny a real
    credit.

    Returns True when any member claimed or holds a balance, False when the
    rule was a no-op (the coverage sweep uses this to prove the wiring is live;
    it never controls flow).
    """
    # DP#25: the ITA s.122.91 arithmetic is Canadian tax law; imported inside
    # the body so this module keeps no jurisdiction import at module scope.
    from countries.canada.training_credit import (
        accrual_for_year, claimable_at, room_after_claim,
        third_bracket_ceiling)

    config = ctx.config
    slots = _taxed_slots(config)
    if not slots:
        return False

    declared_any = any(
        bool(m.get('tuition_by_year'))
        or m.get('training_amount_limit_opening') is not None
        for m in slots)
    if not declared_any:
        # No tuition and no declared opening balance -> nothing to claim and no
        # balance to roll. Strict no-op, DP#32: a household that has never
        # studied and declared no carried-in room is the ordinary household.
        return False

    sim_year = ctx.calendar_year
    tax_provider = (ctx.tax_provider if ctx.tax_provider is not None
                    else default_tax_provider())
    # The CTC is a FEDERAL credit, so its parameters and its brackets are read
    # off the FEDERAL year row -- the same seam every other federal parameter
    # uses (`countries.canada.tax_calc._load_fed_data`, #416's ceiling module).
    # Reading the PROVINCE row instead is the trap: the province rows carry the
    # same federal brackets but their own copy of every other field, so a
    # parameter added only to the federal table silently arrives as 0.0 for a
    # Quebec or Ontario household -- a credit that never fires, with no error.
    fed_data = tax_provider._load_year(sim_year, 'canada', 'federal')
    annual_accrual = fed_data.ctc_annual_accrual
    # A year with no published lifetime cap is a year with no CTC parameters:
    # the pure functions treat `lifetime_cap <= 0` as "the caller supplied no
    # cap", which is the right contract for a library but the wrong one here --
    # in the fold a zero means the DATA is missing, and an unbounded claim
    # computed from a missing ceiling is exactly the plausible-from-absent shape
    # this repo refuses.
    lifetime_cap = fed_data.ctc_lifetime_cap
    # CRA's earnings-limits table is labelled by the TAXATION YEAR whose limit
    # the row supports, but the income it tests is the PRECEDING year's: the
    # row for 2026 reads "to accumulate the annual training amount limit of
    # $250 for the 2026 taxation year ... have total working income of $12,058
    # or more IN 2025 ... net income for 2025 that did not exceed $177,882".
    # So the threshold and the net-income ceiling that apply to THIS year's
    # income are the NEXT year's row -- a one-year shift that reads as a bug
    # until you have the table in front of you, and over-accrues by about one
    # year's indexation if you get it wrong. `sim_year + 1` is a real year in
    # the data or a projection of it, never an absence.
    next_year_data = tax_provider._load_year(sim_year + 1, 'canada', 'federal')
    working_threshold = next_year_data.ctc_working_income_threshold
    ceiling = third_bracket_ceiling(next_year_data.federal_brackets)

    if lifetime_cap <= 0.0:
        return False
    rooms = ws.opening_training_amount_limit
    claims = {}
    new_rooms = {}
    for member in slots:
        role = member.get('role')
        # An explicit `is None` test, never `or`: a member with no declared id
        # still needs a key in the per-member ledger, and `role` is the stable
        # fallback -- but the two are different facts and `or` would make that
        # choice invisible (DP#32).
        member_id = member.get('id')
        mid = role if member_id is None else member_id
        tuition_by_year = member.get('tuition_by_year')
        if tuition_by_year is None:
            tuition_by_year = {}  # no declared tuition -> no per-year map (DP#32)
        # `is None`, never `or`: a declared $0 balance and an absent one price
        # the same but are different facts, and the difference is that an absent
        # one is the limit starting empty rather than a figure somebody stated.
        opening_declared = member.get('training_amount_limit_opening')
        if opening_declared is None:
            opening_declared = 0.0
        birth_year = member.get('birth_year')
        if not _is_usable_birth_year(birth_year):
            raise ValueError(
                f"Member {mid!r} declares tuition or a Canada training amount "
                f"limit but has no `birth_year`. The Canada Training Credit "
                f"(ITA s.122.91) is age-gated -- the limit only accrues between "
                f"25 and 65, and the credit can only be claimed from 26 -- so an "
                f"unknown age is not a $0 credit: it is a missing input that "
                f"would read as 'too young' and silently deny a real credit. "
                f"State the birth year as a number (issue #372). Got "
                f"{birth_year!r}."
            )
        age_at_year_end = sim_year - int(birth_year)
        eligible_tuition = tuition_by_year.get(sim_year, 0.0)
        room = rooms.get(mid)
        if room is None:
            room = float(opening_declared)
        claim = claimable_at(age_at_year_end, room, eligible_tuition,
                             lifetime_cap=lifetime_cap,
                             claimed_to_date=max(0.0, lifetime_cap - room))
        # This year's income -- the accrual test for the room that NEXT year's
        # claim draws on. `*_income_pre` is the member's employment income for
        # the year as computed, before the retirement transition zeroes it, so a
        # member past retirement_age still reports the wages they actually
        # earned (CRA's "working income" test is about the year that was worked,
        # not about whether the model thinks they are retired).
        working_income = (ctx.primary_income_pre if role == 'primary'
                          else ctx.spouse_income_pre)
        net_income = (ctx.primary_taxable_income if role == 'primary'
                      else ctx.spouse_taxable_income)
        accrual = accrual_for_year(age_at_year_end, working_income, net_income,
                                   annual_accrual, working_threshold, ceiling)
        new_room = room_after_claim(room, claim, accrual, lifetime_cap)
        if age_at_year_end >= 65:
            # ITA s.122.91(4): the unused balance expires at the end of the year
            # the individual turns 65, so nothing is CARRIED out of any year at
            # or past that age. The condition is `>= 65` rather than `== 65`
            # deliberately: a member who is already 66 when the projection
            # begins and declares an opening balance may still CLAIM against it
            # (the claim above is computed from the carried-in room first), and
            # then it is gone -- there is no year in which a past-65 member is
            # entitled to carry one forward.
            new_room = 0.0
        new_rooms[mid] = new_room
        claims[role] = claim

    ws.ctc_claimed_primary = claims.get('primary', 0.0)
    ws.ctc_claimed_spouse = claims.get('spouse', 0.0)
    ws.new_training_amount_limit = new_rooms
    return bool(ws.ctc_claimed_primary or ws.ctc_claimed_spouse
                or any(r > 0.0 for r in new_rooms.values()))
