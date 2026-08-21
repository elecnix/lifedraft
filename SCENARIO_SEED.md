# Optimization Scenario Seed Document

Test scenarios sourced from Canadian personal finance communities (RedFlagDeals, Canadian Money Forum, Reddit r/PersonalFinanceCanada), CIBC research (Jamie Golombek), Globe & Mail, and mortgage broker blogs. All data is **fake** — no personal information. Round numbers throughout.

---

## 1. Smith Manoeuvre Scenarios

### 1.1 Classic Smith Manoeuvre — High-Earner, Big Mortgage
**Source**: RedFlagDeals Smith Manoeuvre thread (3,800+ posts), KB Mortgages optimization guide

> "I have a $1.1M mortgage at P-0.7%, $200K in non-reg investments, and $650K house value. Should I liquidate the non-reg, pay down the mortgage, and re-borrow via HELOC to make the interest deductible?"
> — RFD user, Toronto

**Fake test data**:
- Primary: $180,000/yr (48% MTR), Spouse: $40,000/yr (24% MTR)
- House: $800,000, Mortgage: $500,000 @ 4.5%, LTV 62.5%
- HELOC available at P+0.5% = 5.45%
- Non-reg portfolio: $200,000 (cost basis $150,000)
- TFSA room: $80,000 unused, RRSP room: $120,000 unused

**Key decision points to model**:
1. Debt swap: Liquidate non-reg → pay down mortgage → re-borrow and invest. Capital gains tax on $50,000 gain = ~$12,500 at 50% inclusion + marginal rate. But HELOC interest becomes deductible at $10,900/yr (5.45% × $200,000) saving ~$5,230/yr in tax. Break-even ~2.4 years.
2. HELOC vs mortgage rate spread: At 5.45% HELOC after-tax cost = 2.83% (at 48% MTR). Mortgage at 4.5% non-deductible. Net cost: HELOC is **cheaper** after tax.
3. Readvanceable mortgage needed: Scotia STEP, TD FlexLine, Manulife One, NBC All-In-One
4. Capitalizing interest: With readvanceable HELOC, interest can be capitalized (borrow to pay interest). CRA allows this if tracing is clean.

**What our simulator should test**:
- [ ] Debt swap net benefit (after capital gains tax on disposition)
- [ ] HELOC after-tax rate vs mortgage rate comparison
- [ ] Break-even time for debt swap
- [ ] Interest capitalization cash flow impact
- [ ] Risk: market downturn while leveraged

---

### 1.2 Smith Manoeuvre Without Readvanceable Mortgage
**Source**: RFD user asking about Tangerine HELOC, KB Mortgages article

> "Can I do the SM without a readvanceable HELOC? My lender doesn't offer one."
> — RFD user

**Fake test data**:
- Primary: $100,000/yr (38% MTR)
- House: $500,000, Mortgage: $200,000 @ 4.0%
- Standalone HELOC: $200,000 at P+0.5%
- Non-readvanceable: HELOC limit doesn't grow as mortgage is paid down

**Key nuance**: Without readvanceable, you have a fixed HELOC room. You can still do the SM but must:
1. Start with enough HELOC room to last until mortgage renewal (e.g., 4-5 years)
2. At renewal, switch to readvanceable product
3. Until then, you're "investing with your HELOC" rather than true SM (debt level increases initially)

**What our simulator should test**:
- [ ] Fixed HELOC room exhaustion timeline
- [ ] Cost of waiting vs switching now (IRD penalty vs rate savings)
- [ ] Debt trajectory: total debt increases initially without readvancing (principal paydown doesn't become investable)

---

### 1.3 Smith Manoeuvre + Margin Leverage ("Leverage on Leverage")
**Source**: RFD thread "Smith Manoeuvre plus margin" (Aug 2025)

> "I'm doing SM with RBC Homeline. I also want to use 25% margin in my Wealthsimple account because their margin rates are decent. But HELOC deposits pay down margin first — does that break SM traceability?"
> — RFD user

**Fake test data**:
- HELOC balance: $150,000 at P+0.5% (deductible)
- Wealthsimple margin: $50,000 at 4.0% (also deductible)
- Combined investment account: $200,000 in XEQT
- MTR: 43%

**Key CRA rule (ITA s.20(3))**: Refinancing transactions — using borrowed money to repay money previously borrowed means the new borrowing keeps the same purpose. So HELOC → margin → invest is fine for deductibility. But tracing must be clean: separate accounts or documented flows.

**What our simulator should test**:
- [ ] Combined HELOC + margin leverage model
- [ ] Account separation risk (contaminated tracing)
- [ ] Margin call scenario under market stress (-40%)
- [ ] Net after-tax cost of double leverage
- [ ] Risk: both HELOC and margin called simultaneously

---

### 1.4 HELOC Rate vs Mortgage Rate: When SM Doesn't Make Sense
**Source**: RFD user at 1.3% variable + 3.45% HELOC

> "With mortgage at 1.3% and HELOC at 3.45%, does SM make sense? The tax savings don't cover the interest difference."

**Fake test data**:
- Mortgage: $300,000 @ 1.5%
- HELOC: $200,000 @ 3.95%
- MTR: 43%
- HELOC after-tax: 3.95% × (1 - 0.43) = 2.25%
- Mortgage: 1.5% non-deductible
- Spread: HELOC costs 0.75% more after tax

**What our simulator should test**:
- [ ] Rate spread threshold where SM flips from beneficial to costly
- [ ] At what expected investment return does SM break even given rate spread
- [ ] Sensitivity: how many bp of HELOC rate increase kills SM

---

### 1.5 Cash Damming with Rental Property
**Source**: RFD/Ed Rempel Cash Dam explanation

> "If you own rental properties, run all rental expenses through the HELOC and use rental income to pay down your mortgage. Creates deductible debt without new investment risk."

**Fake test data**:
- Primary: $120,000/yr, rental property: $24,000/yr gross rent
- Rental expenses: $18,000/yr (mortgage interest $8,000, tax $4,000, insurance $2,000, maintenance $4,000)
- Principal residence mortgage: $300,000 @ 4.5%
- HELOC: $100,000 at P+0.5%

**Cash dam process**:
1. Pay rental expenses from HELOC ($18,000/yr)
2. Use gross rent ($24,000) to pay down principal residence mortgage
3. Net effect: $18,000 of non-deductible debt → deductible, plus rental income still fully declared
4. Benefit is ~1/6 of full SM (purely tax strategy, no leverage)

**What our simulator should test**:
- [ ] Cash dam annual benefit at different MTRs
- [ ] HELOC balance growth from capitalized expenses
- [ ] Compare: Cash dam vs Smith Manoeuvre vs combined
- [ ] Rental income attribution rules (if spouse owns rental)

---

### 1.6 Dividend Diversion (Debt Conversion Without New Risk)
**Source**: KB Mortgages article

> "Use non-reg dividends to pay down mortgage, then re-borrow and reinvest. Transforms non-deductible debt with no new investment risk."

**Fake test data**:
- Non-reg portfolio: $100,000 generating $4,000/yr in eligible dividends
- Primary mortgage: $250,000 @ 4.5%
- HELOC: available at P+0.5%

**Process**:
1. Receive $4,000 dividend
2. Pay down $4,000 of non-deductible mortgage
3. Re-borrow $4,000 from HELOC
4. Invest $4,000 in same (or different) income-producing investment
5. HELOC interest on $4,000 is now deductible

**What our simulator should test**:
- [ ] Annual conversion rate ($4,000/yr → deductible over time)
- [ ] ROC (return of capital) handling — must not be diverted for personal use
- [ ] Cumulative deductible debt after 10 years of dividend diversion

---

## 2. RRSP vs TFSA Prioritization

### 2.1 Early-Career Low Earner — TFSA First
**Source**: CIBC Jamie Golombek "Blinded by the Refund", multiple Reddit threads

> "I'm 26 making $45,000. RRSP gives a 20% refund. TFSA gives tax-free growth forever. Which first?"
> — Common Reddit question

**Fake test data**:
- Age: 26, Income: $45,000 (MTR ~20%)
- Expected retirement income: $70,000 (MTR ~30%)
- Available: $7,000/yr to invest
- RRSP deduction at 20% saves $1,400 → TFSA gets $5,600 after tax
- If RRSP: contribute $7,000, get $1,400 refund, invest refund in TFSA
- If TFSA: contribute $5,600 (after tax), no refund

**Key insight**: At equal contribution/withdrawal rates, RRSP and TFSA produce identical results. But MTR 20% now vs 30% at withdrawal means **TFSA wins** — you'd deduct at 20% and pay tax at 30%.

**What our simulator should test**:
- [ ] MTR now vs retirement MTR comparison
- [ ] RRSP refund reinvestment loop
- [ ] CCB/GIS impact of RRSP deductions (income-tested benefits)
- [ ] 30-year projection for both choices

---

### 2.2 Mid-Career High Earner — RRSP First
**Source**: Bits & Bonds, YieldMaple, Northern Nest Egg

> "At $110,000, every $10,000 RRSP contribution saves $4,000 in tax. Hard to argue with that."

**Fake test data**:
- Age: 40, Income: $110,000 (MTR ~43%)
- Employer match: 5% on $6,000 = $300/yr (negligible for this test)
- Expected retirement MTR: ~28%
- Available: $15,000/yr

**Strategy**: Contribute to RRSP, then put refund into TFSA.

**What our simulator should test**:
- [ ] RRSP deduction at 43% → withdraw at 28% = 15% tax arbitrage
- [ ] RRSP→TFSA refund loop over 25 years
- [ ] OAS clawback risk from large RRIF mandatory withdrawals at 71+
- [ ] Bracket edge: contribute just enough to drop to lower bracket

---

### 2.3 RRSP Deduct Later Strategy
**Source**: Globe & Mail (Feb 2026), Shajani CPA, RFD forum consensus

> "Contribute now, deduct later. You get tax-sheltered growth immediately and save the deduction for when you're in a higher bracket."

**Fake test data**:
- Age: 30, Income: $65,000 (MTR ~30%)
- Expected income in 5 years: $120,000 (MTR ~43%)
- RRSP room: $80,000 accumulated
- Current savings: $10,000/yr

**Strategy**: Contribute $10,000/yr to RRSP but defer the deduction. Claim it in 5 years when MTR is 43% instead of 30%. Each $10,000 deduction saves $4,300 instead of $3,000 = +$1,300.

**What our simulator should test**:
- [ ] Time value of deferring deduction (lost refund compounding)
- [ ] Break-even: how many years of deferral still wins?
- [ ] AMT (Alternative Minimum Tax) impact at high deduction years
- [ ] Compare: contribute now/deduct later vs TFSA now/RRSP later

---

### 2.4 Spousal RRSP — Income Splitting
**Source**: Life Money, Canadian Money Help, RFD threads

> "My wife earns $30,000 and I earn $140,000. Spousal RRSP lets me deduct at 43% and she withdraws at 20% in retirement."

**Fake test data**:
- Primary: $140,000 (MTR ~43%), Spouse: $30,000 (MTR ~20%)
- Bracket gap: 23pp
- Spousal contribution: $10,000/yr
- 3-year attribution rule applies

**Key rule**: Last spousal contribution December 2026 → first safe withdrawal January 2029. January 2027 contribution → safe January 2030. **December is always better than January.**

**What our simulator should test**:
- [ ] Spousal RRSP benefit = contribution × bracket_gap over working years
- [ ] Attribution risk: early retirement at 55-64 (before pension splitting at 65)
- [ ] Compare: Spousal RRSP vs pension income splitting at 65+
- [ ] HBP from spousal RRSP (exempt from attribution)
- [ ] Contribution in December vs January (attribution clock difference)

---

## 3. Refinance Decision Scenarios

### 3.1 Refinance to Access Equity for Investment
**Source**: RFD thread, MBrowne calculator, KB Mortgages

> "House worth $800K, mortgage $300K. Should I refinance to 80% LTV ($640K) and invest the $340K cash-out?"

**Fake test data**:
- House: $800,000, Mortgage: $300,000 @ 4.5%
- 80% LTV = $640,000, Cash-out: $340,000
- HELOC rate: P+0.5% = 5.45%
- After-tax HELOC cost at 43% MTR: 3.1%
- Expected investment return: 7%
- IRD penalty: ~$8,000

**Key question**: Does the after-tax loan cost (3.1%) beat expected return (7%) by enough to justify the risk and penalty?

**What our simulator should test**:
- [ ] Net benefit after IRD penalty
- [ ] Rate stress: what if HELOC hits 8%? After-tax = 4.56%
- [ ] Monthly cash flow impact of interest-only HELOC payments
- [ ] Compare: refinance vs keep current mortgage + invest from cash flow

---

### 3.2 Variable vs Fixed Rate at Refinance
**Source**: RFD, multiple broker blogs

> "Broker offers 3yr fixed at 4.04% or 5yr variable at 3.75%. Which path over 10 years?"

**Fake test data**:
- Mortgage: $400,000, Amortization: 25 years
- Option A: 3yr fixed at 4.04%, renewal at assumed 5.0%
- Option B: 5yr variable at 3.75%, renewal at assumed 4.5%
- Current rate: 4.95%

**What our simulator should test**:
- [ ] Payment at each rate change (amortization recalculates)
- [ ] Total interest paid under each path
- [ ] Rate sensitivity: what if renewal is 6%? 3.5%?
- [ ] Variable rate with BoC rate changes mid-term

---

### 3.3 Breaking a Mortgage for Readvanceable Product
**Source**: RFD user (blazervault), considering switch from RBC to CIBC

> "RBC doesn't readvance the HELOC above 65% LTV. CIBC readvances at $0.65 on the dollar."

**Fake test data**:
- Current: RBC Homeline, mortgage $450K @ 4.0%, 2 years into 5-year term
- IRD penalty: ~$15,000
- CIBC readvanceable: mortgage $450K @ 4.2%, HELOC readvances fully
- House value: $700,000

**What our simulator should test**:
- [ ] SM benefit lost by not having readvanceable for 3 years
- [ ] Does SM benefit > IRD penalty within remaining term?
- [ ] RBC partial readvance ($0.65/$1) vs CIBC full readvance

---

## 4. RESP Scenarios

### 4.1 Child Approaching Age 17 — Last Year of CESG
**Source**: Our existing model, RFD RESP threads

**Fake test data**:
- Child age 16, turning 17 in March
- RESP balance: $35,000
- CESG received: $5,000 of $7,200 lifetime limit
- Family income: $150,000 (no additional CESG)
- Remaining CESG room: $2,200

**Urgency**: Must contribute before child turns 17 to get the last CESG grant. Only 2,500 × 20% = $500 this year. Don't wait.

**What our simulator should test**:
- [ ] Per-child age-based CESG eligibility cutoff
- [ ] Catch-up provisions (carry-forward of unused room)
- [ ] QESI additional 10% on first $2,500 for Quebec residents
- [ ] Optimal contribution: $2,500 to maximize grants vs $5,000 to use room

---

### 4.2 RESP Over-17 — No Matching, Still Valuable
**Source**: Our model, CRA rules

**Fake test data**:
- Child age 18, in university
- RESP balance: $45,000
- No more CESG/QESI available
- EAP (Educational Assistance Payment) withdrawal: $10,000/yr
- EAP taxable portion: 60% (CESG + QESI + growth)
- Child's income: $8,000 (part-time) + $10,000 EAP = $18,000
- Child's MTR: ~0% (basic personal amount covers it)

**Key insight**: EAP withdrawals taxed in child's hands at presumably very low rate. But the "taxable portion" includes all grants and growth, not contributions (contributions are returned tax-free to subscriber).

**What our simulator should test**:
- [ ] EAP vs PSE (Post-Secondary Education) withdrawal split
- [ ] Tax impact on student with zero other income
- [ ] Timing: spread EAP over multiple years to stay in low bracket
- [ ] What if student income is higher (summer job, co-op)?

---

## 5. Retirement Drawdown Scenarios

### 5.1 OAS Clawback Management
**Source**: CIBC Golombek, Life Money, multiple sources

> "If my RRIF minimum withdrawals push me over $95,323 net income, I lose 15¢ of OAS per dollar above. At $154K I lose all OAS ($8,908/yr)."

**Fake test data**:
- Age 72, RRIF balance: $800,000
- Minimum withdrawal (5.28%): $42,240
- CPP: $15,000, OAS: $8,908
- Other income: $40,000 (pension, investments)
- Total: $106,148 → $10,825 over clawback threshold → lose $1,624 OAS

**What our simulator should test**:
- [ ] Draw from TFSA instead of RRIF to stay under threshold
- [ ] Pension income splitting with spouse (50% of RRIF eligible at 65+)
- [ ] Defer CPP to 70 (higher CPP but same OAS threshold)
- [ ] Model: full RRIF minimums vs strategic TFSA bridge years 65-72

---

### 5.2 RRSP Melt-Down Before 65
**Source**: Ferguson Financial, Life Money

> "Convert RRSP to RRIF at 65, take only minimums, supplement with TFSA. Keeps taxable income low for OAS/clawback purposes."

**Fake test data**:
- Age 60, RRSP: $1,200,000
- Spouse RRSP: $400,000
- Combined: $1,600,000
- Plan: retire at 60, draw from non-reg and TFSA first, defer RRSP to 65, then convert to RRIF and take minimums + pension split

**What our simulator should test**:
- [ ] Pre-65 drawdown order: TFSA → non-reg → RRSP (last)
- [ ] At 65: RRIF minimums + pension splitting
- [ ] OAS clawback avoidance through income control
- [ ] CPP timing: 60 vs 65 vs 70

---

## 6. Combined / Integration Scenarios

### 6.1 Full Family Optimization
**Source**: Our existing use case, RFD multi-topic threads

**Fake test data**:
- Primary: $130,000 (MTR 45.7%), Spouse: $50,000 (MTR 25.7%)
- House: $750,000, Mortgage: $300,000 @ 4.5%
- Readvanceable HELOC at P+0.5%
- RRSP room: $150,000 primary, $50,000 spouse
- TFSA room: $80,000 combined
- 2 children: age 10 (CESG+QESI eligible), age 17 (last year CESG)
- Savings rate: 20% = $36,000/yr
- Employer RRSP match: 3% on $130,000 = $3,900/yr

**Strategy mix**:
1. Capture employer match ($3,900 to group RRSP)
2. Smith Manoeuvre (readvance HELOC, invest, capitalize interest)
3. RRSP for bracket-edge deduction
4. Spousal RRSP for income splitting
5. TFSA for tax-free growth + OAS protection
6. RESP with per-child matching logic

**What our simulator should test**:
- [ ] 10-year projection combining all strategies
- [ ] Rank strategies by net benefit
- [ ] Sensitivity: savings rate 10-30%, return 4-10%, renewal rates 3.5-6.5%

---

### 6.2 New Job with Major Raise — Re-optimization
**Source**: Our anchor scenario (current vs new job)

**Fake test data**:
- Current: $100,000 (MTR 36%)
- New job: $170,000 (MTR 48%)
- MTR jump: +12pp → RRSP deduction worth 48¢/dollar instead of 36¢
- Bracket gap with spouse ($40,000) widens from 16pp to 28pp
- Spousal RRSP benefit: $280 per $1,000 vs $160 per $1,000
- New savings capacity: $34,000/yr vs $20,000/yr

**What our simulator should test**:
- [ ] RRSP becomes much more valuable at higher MTR
- [ ] Spousal RRSP benefit increases with wider bracket gap
- [ ] Should you retroactively claim deferred deductions?
- [ ] CCB impact (higher income reduces benefits)
- [ ] Lifestyle creep risk

---

### 6.3 Divorce / Separation — Attribution Rules Change
**Source**: Life Money, TaxTips.ca, Ferguson Financial

**Fake test data**:
- Primary: $150,000, Spouse: $45,000
- Combined RRSPs: $600,000 (split 70/30)
- Spousal RRSP: $80,000 in spouse's name
- Attribution rule ceases on separation
- RRSP assets are family property (equalization)

**What our simulator should test**:
- [ ] Attribution rules cease in year of separation
- [ ] Spousal RRSP becomes regular RRSP after equalization
- [ ] Tax implications of RRSP transfer under ITA s.146.3
- [ ] No attribution = spouse can withdraw immediately at lower rate

---

## 7. Province-Specific Scenarios

### 7.1 Quebec: Interest Deduction Limited to Investment Income
**Source**: RFD user in Quebec, Ed Rempel blog

> "In Quebec, you can only deduct HELOC interest up to the amount of investment income earned in the year. This changes the SM calculation significantly."

**Fake test data**:
- Quebec resident, MTR: 47.46% at $150,000
- HELOC interest: $10,000/yr
- Dividend income from SM portfolio: $3,000/yr
- Only $3,000 of the $10,000 interest is deductible in Quebec
- Rest carries forward to future years

**What our simulator should test**:
- [ ] Quebec-specific interest deduction carry-forward
- [ ] Prefer Canadian dividend stocks for Quebec SM (generates deductible income)
- [ ] Compare: dividend portfolio vs growth portfolio in Quebec

### 7.2 Alberta: No Provincial Sales Tax, Different Bracket Structure
**Fake test data**:
- Alberta MTR at $150,000: ~39%
- Ontario MTR at $150,000: ~43%
- Same contribution, different tax savings
- AB: $10,000 RRSP saves $3,900
- ON: $10,000 RRSP saves $4,300

**What our simulator should test**:
- [ ] Per-province MTR comparison table
- [ ] Province-switching scenario (move from QC to AB)
- [ ] Provincial clawback differences (Alberta OAS, QC QPP)

---

## 8. Stress Test Scenarios

### 8.1 Market Crash While Leveraged
**Source**: RFD TuxedoBlack, consensus risk warnings

> "If you borrow $200K and the market falls 40%, will you sell? Add to it? Your total debt stays the same but portfolio is worth $120K. HELOC might get called if property values drop too."

**Fake test data**:
- HELOC: $200,000, Investment: $200,000 → drops to $120,000
- HELOC interest: $10,900/yr, still deductible
- House drops 15%: LTV increases, HELOC room might shrink
- Margin call risk on leveraged portion

**What our simulator should test**:
- [ ] 2008-style crash: -40% in year 1, recovery over 5 years
- [ ] HELOC called if LTV exceeds threshold
- [ ] Forced liquidation at loss + tax on capital gains if any
- [ ] Recovery time: how long to get back to even

### 8.2 Interest Rate Spike
**Source**: 2022-2023 rate hike experience

> "My HELOC went from 2.45% to 7.2% in 18 months. Interest costs tripled."

**Fake test data**:
- HELOC balance: $150,000
- Rate path: 2.45% → 4.95% → 7.20% over 18 months
- Monthly interest payment: $306 → $618 → $900
- Cash flow stress: can the household absorb $594/mo increase?

**What our simulator should test**:
- [ ] Rate stress path with BoC rate scenarios
- [ ] Cash flow adequacy at various HELOC rates
- [ ] Break-even: at what rate does SM cost exceed expected return?
- [ ] Capitalized interest buffer: how long until HELOC room runs out?

---

## 9. Asset Location Optimization Scenarios

### 9.1 After-Tax Asset Location — Bonds in RRSP vs TFSA
**Source**: PWL Capital (Benjamin Felix "Optimal Asset Location" 2019), Canadian Portfolio Manager (Justin Bender), Burkett Asset Management (2026)

> "Your after-tax asset allocation drives most of your after-tax returns, NOT your asset location decision." — Benjamin Felix, PWL Capital

**Core insight**: The conventional wisdom "put bonds in RRSP" is misleading. It works under pre-tax allocation constraints, but when controlled for after-tax allocation, the benefit comes from being more aggressive after-tax, not from tax efficiency of location itself.

**Key findings from research**:
1. **After-tax RRSP acts like a TFSA**: $100,000 RRSP at 40% tax = $60,000 after-tax, growing tax-free. The remaining $40,000 + growth belongs to the government.
2. **Pre-tax vs after-tax allocation matters**: Holding bonds in RRSP makes pre-tax 60/40 look like after-tax 75/25 (more aggressive). The "benefit" is really from higher equity exposure after-tax.
3. **Yield drives optimal location**: Per Dammon, Spatt & Zhang (2004), the asset with the **highest yield** should go in the tax-deferred account. When bond yields < equity yields (common today), equities should go in RRSP.
4. **Foreign withholding tax**: US-listed ETFs in RRSP avoid 15% withholding (treaty benefit). In TFSA, withholding is unrecoverable (~22 bps for XEF). This partially offsets or exceeds asset location benefit.
5. **Behavioural risk**: After-tax optimal (stocks in RRSP) results in more aggressive **pre-tax** allocation — the "what you see" drawdown is bigger, even though after-tax drawdown is identical.

**Fake test data**:
- TFSA: $60,000, RRSP: $100,000 (40% tax rate → $60,000 after-tax)
- Total after-tax: $120,000, Target: 50/50 after-tax = $60,000 stocks + $60,000 bonds
- Strategy A (same mix): 50/50 in both → after-tax 50/50 = $180,591 after 10 years at 6%/2%
- Strategy B (stocks in TFSA first, pre-tax 50/50): after-tax 60/40 = $187,453 (but it's the allocation, not location)
- Strategy C (stocks in TFSA first, after-tax 50/50): after-tax 50/50 = $180,591 (identical to A!)

**Practical recommendations from research**:
- **"Light" approach** (99% of investors): Same asset allocation ETF in all accounts (e.g., XEQT/ZAG mix). Simple, tax-efficient enough.
- **"Ludicrous" approach**: Canadian equities in taxable (dividend tax credit), US-listed US equity ETFs in RRSP (treaty withholding), international equity in TFSA, fixed income fill remaining RRSP room.
- **"Plaid" approach**: Full after-tax optimization with foreign withholding tax calculations per ETF.

**What our simulator should test**:
- [ ] After-tax allocation calculation (RRSP × (1 - expected_tax_rate))
- [ ] Foreign withholding tax drag by account type (RRSP vs TFSA vs non-reg)
- [ ] Pre-tax vs after-tax allocation comparison
- [ ] Asset location impact on 10-year projected wealth
- [ ] Behavioural risk: pre-tax drawdown comparison across location strategies

---

### 9.2 Asset Location by Investment Type — Tax Efficiency Matrix
**Source**: RBC Wealth Management "Tax-efficient asset location" (2025), BMO Tax Tips for Investors 2026, Burkett Insights (April 2026)

**Tax treatment by income type (2026 Quebec combined)**:

| Investment Type | Tax Treatment | Non-Reg Effective Rate (at 45.7% MTR) | Best Account |
|---|---|---|---|
| Canadian interest (bonds, GICs) | Fully taxable at marginal rate | 45.7% | RRSP/TFSA first |
| Foreign interest | Fully taxable + potential withholding | 45.7% + unrecoverable WHT | RRSP (treaty) |
| Canadian eligible dividends | Gross-up + dividend tax credit (~25% effective) | ~25% | Non-reg OK (tax credit) |
| Capital gains | 50% inclusion rate | 22.85% | Non-reg (loss offset, deferral) |
| Foreign dividends (US) | Fully taxable + 15% withholding | 45.7% + 15% WHT | RRSP (treaty eliminates WHT) |
| Foreign dividends (non-US) | Fully taxable + potential WHT | 45.7% + WHT | RRSP or TFSA |
| Return of Capital | Not taxable (reduces ACB) | Deferred as capital gain | Non-reg (ACB tracking) |

**Practical asset location hierarchy**:
1. **RRSP**: Hold interest-bearing investments + US-listed equity ETFs (avoid WHT)
2. **TFSA**: Hold Canadian equities + growth assets + international equity (no WHT on Canadian equities)
3. **Non-registered**: Hold Canadian dividend-paying stocks (dividend tax credit) + capital-gains-oriented investments + return-of-capital distributions
4. **Avoid in non-reg**: Interest income, foreign dividends (no tax credit)

**What our simulator should test**:
- [ ] Tax drag by account type for each investment income type
- [ ] Optimal location assignment algorithm given portfolio composition
- [ ] After-tax return comparison across location strategies
- [ ] Quebec-specific: interest deduction limited to investment income earned

---

### 9.3 Quebec-Specific: Interest Deduction Limited to Investment Income
**Source**: RFD, Ed Rempel blog, Quebec Tax Act

**Key rule for Quebec**: HELOC interest deduction is limited to net investment income earned in the year. Any excess carries forward. This makes **dividend-focused Smith Manoeuvre portfolios** more valuable in Quebec than growth-focused ones.

**Fake test data**:
- Quebec resident, MTR: 47.46% at $150,000
- HELOC interest: $10,000/yr
- Investment income sources:
  - Eligible dividends: $3,000/yr
  - Capital gains (realized): $2,000/yr
- Quebec limits deduction to $5,000 (dividends + capital gains)
- Federal: full $10,000 deductible
- Carry-forward: $5,000 unused Quebec deduction

**Strategy optimization for Quebec**:
- Prefer Canadian dividend-paying stocks in non-reg SM portfolio
- Use DRIP to generate more dividend income
- Consider bond ETFs (interest counts as investment income for Quebec)
- Capital gains only count when realized (not accrued)

**What our simulator should test**:
- [ ] Quebec interest deduction carry-forward balance
- [ ] Federal vs Quebec deduction difference
- [ ] Optimal asset mix for Quebec SM (dividend-heavy vs growth-heavy)
- [ ] Year-by-year carry-forward accumulation and eventual use

---

## 10. FHSA (First Home Savings Account) Scenarios

### 10.1 FHSA vs RRSP HBP vs TFSA for First Home
**Source**: AMF (Autorité des marchés financiers), TD comparison, BLG (Borden Ladner Gervais), Fidelity

**FHSA rules (as of 2026)**:
- $8,000/yr contribution limit, $40,000 lifetime
- Contributions are tax-deductible (like RRSP)
- Growth is tax-free (like TFSA)
- Qualifying withdrawal for first home is tax-free AND never repaid
- Must close by Dec 31 of year following first qualifying withdrawal, or 15th anniversary, or age 71
- Can transfer unused FHSA to RRSP/RRIF without using RRSP room
- Contribution deadline: December 31 (NOT March 1 like RRSP)

**Fake test data**:
- Age 28, first-time home buyer, income: $75,000 (MTR ~35%)
- Timeline: buying in 4 years
- Options to compare:
  1. FHSA: $8,000/yr × 4 = $32,000, tax savings = $11,200, tax-free withdrawal
  2. RRSP HBP: $8,000/yr × 4 = $32,000, tax savings = $11,200, but must repay over 15 years
  3. TFSA: $5,200/yr (after tax) × 4 = $20,800, no tax savings, but tax-free withdrawal
  4. Combined: FHSA + HBP = $60,000 available for down payment

**Double deduction strategy (BLG)**: Withdraw from RRSP under HBP → contribute to FHSA → withdraw tax-free from FHSA. Gets tax deduction on original RRSP contribution + another deduction on FHSA contribution. CRA confirmed this is allowed.

**What our simulator should test**:
- [ ] FHSA vs RRSP HBP vs TFSA net benefit for first home
- [ ] Double deduction strategy (HBP → FHSA)
- [ ] FHSA tax deduction timing (claim in higher income year)
- [ ] What if no home purchased: FHSA → RRSP transfer (no RRSP room used)
- [ ] FHSA + HBP combined for same purchase
- [ ] Spousal FHSA: each spouse opens their own ($80,000 combined lifetime)

---

## 11. TOSI and Income Splitting Scenarios

### 11.1 TOSI Rules for Private Corporation Income
**Source**: CRA Guidance on split income rules, RSM Canada "TOSI: A Practical Approach" (2019), RBC Wealth Management, Tax Partners (2026)

**TOSI applies to**:
- Dividends from private corporations received by "specified individuals"
- Income from partnerships/trusts derived from related businesses
- Certain capital gains from disposition of private company shares
- Interest on debt obligations of private corporations

**TOSI taxes split income at the HIGHEST marginal rate** (effectively eliminating any benefit of splitting).

**Excluded amounts (TOSI does NOT apply)**:
1. **Salary**: TOSI does not apply to salary received by any family member (key carve-out)
2. **Excluded business**: Individual is actively engaged 20+ hrs/week, or was in 5 prior years (non-consecutive OK)
3. **Excluded shares**: Age 25+, owns 10%+ votes AND value, <90% income from services, not professional corp
4. **Reasonable return**: Age 25+, based on labour, property, risk, historical payments
5. **Safe harbour capital return**: Age 18-24, prescribed rate (currently 2%) on contributed capital
6. **Arm's length capital**: Capital contributed from non-related sources
7. **Inherited property**: Step into deceased's shoes for exclusions
8. **Pension splitting exception**: Spouse 65+ can split without TOSI
9. **Public company dividends**: NOT subject to TOSI (listed on designated stock exchange)
10. **QSBC/qualified farm capital gains**: Excluded from split income

**Fake test data for TOSI scenario**:
- Parent owns 100% of Opco (services business, gross revenue $600,000)
- Spouse works part-time as bookkeeper (15 hrs/week)
- Two adult children: Child A (age 22, 5 hrs/week), Child B (age 28, not involved)
- Opco pays $80,000 dividends to parent, $30,000 to spouse, $20,000 to each child

**TOSI analysis**:
- Spouse: 15 hrs/week < 20 bright-line test. But may qualify as "regular, continuous, substantial". CRA would evaluate factors. If TOSI applies, spouse taxed at top rate on $30,000.
- Child A (22): 5 hrs/week doesn't meet bright-line. May qualify for safe harbour capital return if they invested capital. Otherwise TOSI at top rate on $20,000.
- Child B (28): Not active. Excluded shares? Need 10%+ votes AND value. If owns 10%+ and <90% of income from services → excluded. Otherwise: reasonable return test (labour=0, property contributed, risk assumed).

**Practical TOSI avoidance strategies**:
- Pay reasonable salary instead of dividends (TOSI doesn't apply to salary)
- Have family members work 20+ hrs/week to qualify for excluded business
- Structure share ownership for 10%+ votes and value for children 25+
- Separate holdco for second-generation investment income (not from related business)
- Use prescribed-rate loans to trust/corporation (2% safe harbour)

**What our simulator should test**:
- [ ] TOSI applicability check based on age, involvement, ownership
- [ ] Salary vs dividend comparison for family members
- [ ] Excluded shares qualification (90% services test, 10% ownership test)
- [ ] Reasonable return calculation (labour + property + risk)
- [ ] Safe harbour capital return (prescribed rate × capital contributed)
- [ ] Interaction with general attribution rules (s.74.1)

---

### 11.2 Attribution Rules — Spousal and Minor Child Transfers
**Source**: ITA s.74.1, s.74.2, CRA interpretations, Tax Partners (2026)

**Attribution rules apply to**:
- Property transferred (or loaned at < prescribed rate) to spouse: income AND capital gains attribute back to transferor
- Property transferred to minor child (under 18): income attributes back, but NOT capital gains
- Loans at prescribed rate (currently 2%): escape attribution if interest paid within 30 days of year-end

**Key exceptions**:
- Spousal RRSP (s.146.3): 3-year attribution rule — if contribution made, withdrawal within 3 calendar years attributes to contributor
- Prescribed-rate loan strategy: Loan at 2% to spouse, they invest, pay interest by Jan 30 each year. Investment income over 2% is theirs. If rate goes up, existing loans keep the old rate.

**Fake test data — Prescribed rate loan**:
- Primary lends $100,000 to spouse at prescribed rate (2%)
- Spouse invests in Canadian dividend ETF (4% yield)
- Spouse pays $2,000 interest to primary by Jan 30
- Spouse earns $4,000 dividends, net $2,000 after interest
- $2,000 taxed in spouse's hands (low MTR), not attributed
- If interest not paid by Jan 30: ALL income attributes back to primary

**What our simulator should test**:
- [ ] Prescribed-rate loan net benefit calculation
- [ ] Attribution tracking for spousal property transfers
- [ ] Minor child attribution (income only, not capital gains)
- [ ] Interest payment deadline compliance risk
- [ ] Compare: prescribed-rate loan vs spousal RRSP for income splitting

---

## 12. Pension Income Splitting and Retirement Scenarios

### 12.1 Pension Income Splitting at 65+
**Source**: CRA pension income splitting rules, RBC Wealth Management "Pension Income Splitting", Globe & Mail (April 2025), National Bank

**Key rules**:
- Up to 50% of eligible pension income can be split with spouse
- Must file joint election on Form T1032
- At 65+: RRIF/LIF payments, RRSP annuities, foreign pensions (including US SS), registered pension plan payments are eligible
- Under 65: Only registered pension plan life annuity payments (and death-of-spouse payments) qualify
- **Quebec**: Provincial pension splitting only available at 65+ (even for RPP)
- CPP/QPP: NOT eligible for pension splitting, but CPP sharing is available separately
- OAS: NOT eligible for pension splitting

**Pension income credit**: First $2,000 of eligible pension income gets 15% federal non-refundable credit ($300/yr). Both spouses can claim if income is split to give each at least $2,000.

**Fake test data**:
- Spouse A: Age 68, RRIF payments $40,000/yr, CPP $14,000, OAS $8,908. Total: $62,908
- Spouse B: Age 66, CPP $8,000, OAS $8,908, investment income $15,000. Total: $31,908
- Without splitting: Spouse A in higher bracket, Spouse A pays ~$14,500 federal + provincial tax
- With 50% split ($20,000 to B): Spouse A income $42,908, Spouse B income $51,908
- Both claim $2,000 pension credit ($300 each)
- OAS clawback: Splitting may keep Spouse A under clawback threshold

**Strategy: Convert RRSP to RRIF at 65 even if still working**:
- Creates $2,000 pension income credit ($300 savings)
- Enables pension splitting with spouse (even if spouse doesn't have pension income)
- If spouse receives at least $2,000 split pension, they also get the credit ($300 each = $600 combined)
- At 71: mandatory RRIF conversion anyway, but starting at 65 gives 6 extra years of splitting + credits

**What our simulator should test**:
- [ ] Optimal pension split percentage (not always 50%)
- [ ] Pension income credit: both spouses claiming $2,000
- [ ] OAS clawback avoidance through splitting
- [ ] RRIF early conversion at 65 vs 71
- [ ] Quebec: provincial splitting only at 65+
- [ ] CPP sharing vs pension splitting (different programs)
- [ ] Impact on income-tested benefits (GST credit, age amount)

---

### 12.2 CPP/QPP Sharing
**Source**: CRA CPP pension sharing, Service Canada

**Key rules**:
- Available to legal spouses/common-law partners living together
- Portion based on months lived together during joint contributory period
- CPP post-retirement benefit NOT eligible for sharing
- Combined total stays the same — it's about tax splitting, not increasing benefits
- Separate from CRA pension income splitting (different program)

**What our simulator should test**:
- [ ] CPP sharing tax benefit calculation
- [ ] Combined CPP sharing + pension splitting
- [ ] Compare: CPP sharing vs pension splitting for same couple

---

### 12.3 OAS Clawback Detailed Modeling
**Source**: CIBC Golombek, RBC Wealth Management, Service Canada

**2026 OAS clawback thresholds (estimated)**:
- Recovery threshold: ~$95,323 net income
- 15% clawback per dollar above threshold
- Maximum clawback at ~$154,000+ (all OAS lost)
- OAS amount: ~$8,908/yr (at age 65, adjusted quarterly for inflation)

**Strategies to manage OAS clawback**:
1. **Pension income splitting**: Up to 50% of RRIF/pension → lower net income
2. **TFSA withdrawals**: Not counted as income → bridge years 65-72
3. **Defer CPP to 70**: Higher CPP but OAS threshold stays same
4. **Defer OAS to 70**: 0.6% increase per month deferred (36% higher at 70 vs 65)
5. **RRSP melt-down before 65**: Draw down while no OAS yet
6. **Prescribed-rate loan**: Shift investment income to lower-income spouse
7. **Non-reg capital gains**: Only 50% inclusion reduces net income impact

**Fake test data — OAS clawback scenario**:
- Age 72, RRIF: $800,000 (min withdrawal 5.28% = $42,240)
- CPP: $15,000, OAS: $8,908
- Pension: $25,000, non-reg income: $20,000
- Total: $111,148 → $15,825 over threshold → lose $2,374 OAS
- Strategy: pension split $20,000 to spouse → net income $91,148 → no clawback → save $2,374 + get full $8,908

**What our simulator should test**:
- [ ] OAS clawback calculation at various income levels
- [ ] Optimal pension split % to minimize OAS clawback
- [ ] TFSA bridge strategy (draw TFSA first to keep income low)
- [ ] Combined OAS + pension splitting optimization
- [ ] Deferring OAS/CPP: NPV comparison of take at 65 vs 70

---

## 13. Investment Type Tax Implications — Detailed Modeling

### 13.1 Canadian Eligible Dividends — Gross-Up and Tax Credit
**Source**: CRA, BMO Tax Tips 2026

**2026 Federal dividend tax credit**:
- Eligible dividends: 38% gross-up, 15.0198% federal DTO (dividend tax credit)
- Non-eligible dividends: 15% gross-up, 9.0301% federal DTO
- Provincial credits vary (Quebec has its own calculation)

**Effective tax rate on eligible dividends (at $130,000 income, QC combined ~45.7% MTR)**:
- Eligible dividends: ~25-30% effective rate (much lower than marginal)
- Non-eligible dividends: ~35-38% effective rate
- Interest income: 45.7% (full marginal rate)

**Implication for non-registered portfolio**: Canadian dividend stocks have ~15-20 percentage point tax advantage over interest-bearing investments in non-reg accounts.

**What our simulator should test**:
- [ ] Eligible vs non-eligible dividend tax calculation (with gross-up and DTC)
- [ ] Effective tax rate by income level for each investment type
- [ ] Optimal dividend income threshold to avoid bracket creep
- [ ] Quebec-specific dividend tax credit calculation

---

### 13.2 Capital Gains — 50% Inclusion and Tax-Loss Harvesting
**Source**: CRA, BMO Tax Tips 2026

**Key rules**:
- 50% inclusion rate (was temporarily 66.67% for gains over $250K in 2024, but reversed)
- Capital losses can offset capital gains (carry back 3 years, forward indefinitely)
- Superficial loss rule: cannot claim loss if repurchasing identical property within 30 days (self or spouse)
- Capital gains deferral: unrealized gains are not taxed (unlike interest income)
- Deemed disposition at death: all capital gains realized (except spousal rollover)

**Tax-loss harvesting strategy**:
- Before year-end: realize losses to offset gains
- Wait 31 days before repurchasing same security (or buy a similar but not identical ETF)
- Net capital losses can be carried back 3 years or forward indefinitely

**What our simulator should test**:
- [ ] Capital gains inclusion rate impact
- [ ] Tax-loss harvesting timing model
- [ ] Deemed disposition at death tax liability
- [ ] Capital gains reserve (5-year deferral for certain dispositions)
- [ ] Superficial loss avoidance

---

### 13.3 Return of Capital (ROC) and ACB Tracking
**Source**: CRA, RBC Wealth Management

**Key rules**:
- ROC distributions reduce Adjusted Cost Base (ACB), not taxed in year received
- When ACB reaches $0, further ROC is treated as capital gain
- Preferred shares and certain ETFs pay ROC as part of distributions
- Corporate class mutual funds use ROC extensively

**Tax efficiency**: ROC is the most tax-efficient distribution type in non-reg accounts because:
1. Not taxable when received
2. Converts to capital gains when ACB hits $0 (only 50% inclusion)
3. Defers tax indefinitely (hold forever → never taxed)

**What our simulator should test**:
- [ ] ACB tracking over time with ROC distributions
- [ ] Capital gain trigger when ACB reaches $0
- [ ] Compare: ROC-heavy fund vs dividend-heavy fund after-tax returns
- [ ] Non-reg vs registered account for ROC distributions

---

### 13.4 Foreign Withholding Tax by Account Type
**Source**: PWL Capital (Justin Bender "Foreign Withholding Taxes" 2016), BMO Tax Tips 2026

**Withholding tax on foreign income (simplified)**:

| ETF Type | Non-Reg | TFSA | RRSP | Notes |
|---|---|---|---|---|
| Canadian-listed US equity (XUU, VFV) | 15% WHT, recoverable via foreign tax credit | 15% WHT, NOT recoverable | 0% WHT (treaty exemption) | Hold US-listed in RRSP |
| US-listed US equity (ITOT, VTI) | 15% WHT, recoverable | 15% WHT, NOT recoverable | 0% WHT (treaty exemption) | Best in RRSP |
| Canadian-listed intl equity (XEF) | 1 level WHT, partially recoverable | 1 level WHT, NOT recoverable | 1 level WHT, NOT recoverable | Same everywhere |
| US-listed intl equity (IXUS) | 2 levels WHT, partially recoverable | 2 levels WHT, NOT recoverable | 1 level WHT, partially recoverable | Avoid in TFSA |

**Tax drag estimates** (bps per year):
- XUU in TFSA: ~22 bps lost (unrecoverable WHT)
- XEF in TFSA: ~27 bps lost
- VTI (US-listed) in RRSP: 0 bps (treaty exemption)
- Canadian equities (VCN/XIC): 0 bps WHT anywhere

**Optimal ETF placement**:
1. **RRSP**: US-listed US equity ETFs (VTI, ITOT) — avoid all withholding tax
2. **TFSA**: Canadian equity (VCN, XIC) — no WHT, tax-free growth
3. **Non-reg**: Canadian dividend ETFs (VDY, XDIV) — dividend tax credit + capital gains treatment
4. **Avoid in TFSA**: US/international equity (unrecoverable WHT drag)

**What our simulator should test**:
- [ ] Foreign withholding tax by account type and ETF selection
- [ ] WHT tax drag calculation (bp impact on returns)
- [ ] Norbert's gambit cost-benefit analysis (currency conversion for RRSP)
- [ ] Optimal ETF placement across accounts given portfolio targets

---

## 14. BMO Tax Tips 2026 — Key Strategies

### 14.1 Income Splitting with Prescribed-Rate Loans
**Source**: BMO Tax Tips for Investors 2026 Edition

**Strategy**: Lend to lower-income spouse at prescribed rate. Interest must be paid by Jan 30 each year.

**2026 prescribed rate**: 2% (check current — changes quarterly)

**Fake test data**:
- Primary lends $200,000 to spouse at 2%
- Spouse invests at 5% return ($10,000/yr)
- Spouse pays $4,000 interest to primary by Jan 30
- Spouse nets $6,000 taxed at their low rate (20%) = $1,200 tax
- Primary reports $4,000 interest at high rate (45.7%) = $1,828 tax
- Family tax: $3,028
- Without loan: Primary earns $10,000 at 45.7% = $4,570 tax
- **Annual savings: $1,542**

**Key gotcha**: If you miss the Jan 30 interest payment deadline by even 1 day, ALL income attributes back to the lender for that year AND all future years.

**What our simulator should test**:
- [ ] Prescribed-rate loan benefit at various rate/return combinations
- [ ] Compliance risk of missed interest payment
- [ ] Lock-in benefit: prescribed rate stays fixed for the life of the loan even if rates rise
- [ ] Compare: prescribed-rate loan vs spousal RRSP

---

### 14.2 Donating Appreciated Securities
**Source**: BMO Tax Tips 2026

**Key rule**: Donating publicly traded securities with accrued gains to a registered charity eliminates the capital gains tax on the donated portion. You still get a charitable donation receipt for the FMV.

**Fake test data**:
- Non-reg stock with $50,000 FMV, $20,000 ACB → $30,000 capital gain
- Donating stock: $0 capital gains tax + $50,000 charitable receipt
- Selling then donating: $30,000 × 50% inclusion × 45.7% = $6,855 tax + $43,145 net + $43,145 receipt
- **Tax savings from donating in-kind: ~$6,855**

**What our simulator should test**:
- [ ] In-kind donation tax elimination calculation
- [ ] Compare: sell + donate cash vs donate securities in-kind
- [ ] First-time donor's super credit (if still available)

---

### 14.3 Borrowing to Invest — Interest Deductibility Rules
**Source**: BMO Tax Tips 2026, ITA s.20(1)(c), CRA Interpretation Bulletins

**Interest is deductible if ALL conditions met**:
1. **Legal obligation to pay interest**: Must be a bona fide loan
2. **Direct use**: Borrowed funds must be used to earn income from property or business
3. **Reasonable expectation of income**: Must be income-producing investments (not just capital gains)
4. **Reasonable rate**: Market rate of interest
5. **Not for tax-exempt income**: Cannot deduct interest on loans for TFSA, RRSP, RESP

**CRA tracing requirements for Smith Manoeuvre**:
- Dedicated HELOC sub-account for investment borrowing only
- Never mix personal spending with investment borrowing
- Maintain dated log of each advance and corresponding investment purchase
- Keep all statements for audit trail
- If investments sold and proceeds used personally, proportional loss of deduction

**What our simulator should test**:
- [ ] Interest deductibility validation (income-producing purpose)
- [ ] Proportional deduction loss on partial disposition
- [ ] CRA tracing compliance scoring
- [ ] Compare: interest on investment loan vs interest on personal debt

---

## Scenario Priority for Implementation (Updated)

| Priority | Scenario | Why |
|----------|----------|-----|
| P0 | Classic Smith Manoeuvre (1.1) | Core use case, well-documented |
| P0 | RRSP vs TFSA by income (2.1, 2.2) | Most common Canadian question |
| P0 | Full family optimization (6.1) | Integration test, our main use case |
| P0 | Asset location by type (9.1, 9.2) | Major after-tax return driver, commonly misunderstood |
| P1 | Spousal RRSP (2.4) | Attribution rules are complex, our model handles it |
| P1 | Deduct later (2.3) | Unique to our simulator |
| P1 | HELOC vs mortgage rate (1.4) | Clear break-even analysis |
| P1 | Refinance decision (3.1) | Cash-out vs stay put |
| P1 | New job raise (6.2) | Our anchor scenario |
| P1 | Pension income splitting (12.1) | Major retirement optimization |
| P1 | OAS clawback (12.3) | Retirement income management |
| P1 | Investment type tax matrix (9.2, 13.x) | Core tax modeling accuracy |
| P1 | FHSA vs RRSP HBP (10.1) | New account type, complex interaction |
| P1 | Quebec SM interest limit (9.3) | Province-specific, our model targets QC |
| P2 | SM without readvanceable (1.2) | Edge case but common question |
| P2 | SM + margin (1.3) | Advanced, high risk |
| P2 | Cash damming (1.5) | Rental-specific |
| P2 | TOSI rules (11.1) | Private corporation income splitting |
| P2 | Attribution rules (11.2) | Prescribed-rate loan strategy |
| P2 | CPP sharing (12.2) | Separate from pension splitting |
| P2 | Foreign withholding tax (13.4) | ETF placement optimization |
| P2 | Prescribed-rate loans (14.1) | Income splitting with spouse |
| P2 | Donation of securities (14.2) | Tax-efficient giving |
| P2 | Capital gains modeling (13.2) | Tax-loss harvesting, deemed disposition |
| P2 | ROC/ACB tracking (13.3) | Non-reg portfolio accuracy |
| P2 | Quebec SM (7.1) | Province-specific |
| P2 | Market crash (8.1) | Stress testing |
| P2 | Rate spike (8.2) | Stress testing |