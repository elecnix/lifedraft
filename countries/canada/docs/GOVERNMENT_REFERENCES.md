# Government Program References

This document lists authoritative government sources for every program, rule, and
data point modeled in the codebase. Each module's docstring references this file
for the full URL list.

## Master Year-Specific Data Source

RRSP dollar limits, TFSA limits, YMPE, YAMPE (updated annually by CRA):
- **URL**: https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html
- **Key data**: RRSP dollar limit ($33,810 for 2026), TFSA dollar limit ($7,000 for 2024-2026),
  YMPE ($71,300 for 2025; $74,600 for 2026), YAMPE ($81,200 for 2025; $85,000 for 2026)
- **Used by**: tax_data.py, rrsp_ledger.py, strategy.py, fhsa.py, account_models.py

## RRSP (Registered Retirement Savings Plan)
- **ITA Section**: s.146
- **Program page**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/questions-answers-about-contributing-rrsp.html
- **Detailed guide (T4040)**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4040/rrsps-other-registered-plans-retirement.html
- **Data source (contribution limits, YMPE)**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/contributing-a-rrsp-prpp/questions-answers-about-contributing-rrsp.html

## TFSA (Tax-Free Savings Account)
- **ITA Section**: s.146.2
- **Program page**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account.html
- **Contribution room**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account/contributing/calculate-room.html

## FHSA (First Home Savings Account)
- **Legislation**: Bill C-47, Division V of Part 1 (added s.146.6 to ITA)
- **Program page**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account.html
- **Opening/participating**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/opening-your-fhsas.html
- **Participating (contributions/transfers)**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html

## HBP (Home Buyers' Plan)
- **ITA Section**: s.146.4
- **Program page**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan.html
- **Participating**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan/participate-home-buyers-plan.html
- **Withdrawals**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan/withdraw-funds-rrsp-s-under-home-buyers-plan.html

## RESP / CESG / CLB / QESI (Registered Education Savings Plan)
- **ITA Section**: s.146.1 (RESP)
- **CESG (Canada Education Savings Grant)**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html
- **CESG program (ESDC)**: https://www.canada.ca/en/employment-social-development/services/education-savings/grant.html
- **CLB (Canada Learning Bond)**: https://www.canada.ca/en/employment-social-development/services/education-savings/bond.html
- **QESI (Quebec Education Savings Incentive)**: https://justepourtous.revenuquebec.ca/en/subjects/quebec-education-savings-incentive
- **RESP guide (RC4092)**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/rc4092/registered-education-savings-plans-resps.html

## OAS (Old Age Security) / Clawback
- **ITA Section**: Part I, s.56(1)(a) (OAS); s.180.2 (clawback)
- **OAS program page**: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security.html
- **Clawback/recovery tax**: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/recovery-tax.html
- **Repayment calculation**: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/repayment.html

## CPP / QPP (Canada/Quebec Pension Plan)
- **CPP Act**: https://laws-lois.justice.gc.ca/eng/acts/C-8/
- **CPP program page**: https://www.canada.ca/en/services/benefits/publicpensions/cpp.html
- **CPP sharing between spouses**: https://www.canada.ca/en/services/benefits/publicpensions/cpp/share-cpp.html
- **QPP (Retraite Québec)**: https://www.retr.quebec.ca/en
- **CPP contribution rates / YMPE**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contributions.html

## RRIF (Registered Retirement Income Fund)
- **ITA Section**: s.146.3
- **Guide (T4040, shared with RRSP)**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4040/rrsps-other-registered-plans-retirement.html

## Pension Income Splitting (T1032)
- **ITA Section**: s.60.03
- **CRA guide**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/pension-income-splitting.html
- **Form T1032**: https://www.canada.ca/en/revenue-agency/services/forms-publications/forms/t1032.html

## Spousal RRSP Attribution
- **ITA Section**: s.146(8.3)
- **CRA guide (T4040, Ch.5)**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4040/rrsps-other-registered-plans-retirement.html

## Attribution Rules (Spousal Property Transfer)
- **ITA Section**: s.74.1 (spousal attribution)
- **ITA Section**: s.74.2 (minor child attribution)
- **Archived IT-511R**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/it511r/archived-interspousal-certain-other-transfers-loans-property.html

## TOSI (Tax on Split Income)
- **ITA Section**: s.104.2
- **CRA folio**: https://www.canada.ca/en/revenue-agency/services/tax/technical-information/income-tax/folio-xxx.html

## Interest Deductibility (ITA §20(1)(c))
- **ITA Section**: s.20(1)(c)
- **CRA folio S3-F6-C1 (Interest Deductibility)**: https://www.canada.ca/en/revenue-agency/services/tax/technical-information/income-tax/folio-series/folio-s3/s3-f6-c1-interest-deductibility.html

## Quebec Interest Deduction Limit (TP-1 Schedule L)
- **Quebec TP-1 Schedule L instructions**: https://www.revenuquebec.ca/en/online-services/individuals/income-tax-return/tp-1-schedule-l/

## Dividend Tax Credits
- **Federal eligible dividends**: ITA s.82(1) (gross-up 38%), s.121 (federal DTC 15.0198%)
- **Federal non-eligible dividends**: ITA s.82(1)(b) (gross-up 15%), s.121 (federal DTC 9.0301%)
- **Provincial DTCs**: Vary by province; see provincial tax forms
- **CRA reference**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account.html (dividend tax credit info in T1 guide)

## Capital Gains Inclusion Rate
- **ITA Section**: s.38, s.39, s.40
- **Inclusion rate**: 50% (one-half) for individuals; 2/3 for gains realized after June 24, 2024 on properties other than excluded property
- **CRA reference**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/capital-gains.html

## Foreign Withholding Tax
- **ITA Section**: s.126 (foreign tax credit)
- **CRA reference**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-return/line-40500-foreign-tax-credit.html

## Prescribed Rate Loans
- **ITA Section**: s.74.5(2), s.80.4(1)
- **CRA prescribed rates**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/benefits-allowances/financial/loans-interest-free-low-interest.html
- **Archived IT-511R**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/it511r/archived-interspousal-certain-other-transfers-loans-property.html

## Federal Tax Brackets
- **Data source**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html

## Quebec Provincial Tax
- **Data source**: https://www.revenuquebec.ca/en/individuals/income-tax-rates/
- **Quebec abatement (16.5%)**: ITA s.8(1)
- **TP-1 form**: https://www.revenuquebec.ca/en/individuals/income-tax-return/

## Ontario Provincial Tax
- **Data source**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/gst-hst-place-supply/chart-place-supply-ontario.html
- **Ontario surtax**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html
- **Surtax thresholds (2025/2026)**: https://www.taxtips.ca/taxrates/on.htm
- **Ontario Health Premium**: https://www.ontario.ca/page/health-premium
- **Ontario Trillium Benefit / Ontario Sales Tax Credit (OSTC)**: https://www.canada.ca/en/revenue-agency/services/child-family-benefits/provincial-territorial-programs/province-ontario.html
- **Ontario LIFT (Low-income Individuals and Families Tax) credit**: https://www.ontario.ca/page/low-income-workers-tax-credit
- **Used by**: countries/canada/provinces/ontario.py, countries/canada/provinces/ontario_credits.py

## Bank of Canada Rates
- **Data source**: https://www.bankofcanada.ca/rates/interest-rates/
- **Policy interest rate**: https://www.bankofcanada.ca/rates/interest-rates/canadian-interest-rates/
- **BoC Valet API (prime rate series)**: https://www.bankofcanada.ca/valet/observations/V39079
- **Key data**: Prime rate 6.95%, overnight rate 4.75% (as of 2025-12 fallback defaults)
- **Used by**: boc_data.py, rate_model.py

## IRD (Interest Rate Differential) Mortgage Penalty
- **Not a government program** — lender-specific calculation based on posted rates
- **Reference**: FCAC mortgage breakup guide https://www.canada.ca/en/financial-consumer-agency/services/mortgages/break-mortgage.html

## HELOC Tracing Rules
- **ITA Section**: s.20(1)(c) (interest deductibility requires direct use of borrowed money for income-earning purpose)
- **CRA folio S3-F6-C1**: https://www.canada.ca/en/revenue-agency/services/tax/technical-information/income-tax/folio-series/folio-s3/s3-f6-c1-interest-deductibility.html

## CPP Enhancement / CPP2 (Second Additional Contributions)
- **Legislation**: CPP Act s.11.2, s.16, s.17.1, s.18.1; Schedule 2
- **CRA CPP enhancement page**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contributions.html
- **Second additional CPP (CPP2) rates and maximums**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
- **CPP enhancement overview**: https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-enhancement.html
- **Key data**: YMPE2 (YAMPE) $81,900 for 2026, $81,200 for 2025; CPP2 rate 4% employee/employer, 8% self-employed
- **Used by**: cpp_sharing.py (compute_cpp2_contribution, compute_cpp2_benefit), tax_data.py (cpp_rate, YMPE), retirement.py (CPP_OAS_BY_YEAR)

## CPP/QPP Contribution Rates and Basic Exemption
- **CRA CPP contribution rates**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contributions.html
- **Key data**: CPP rate 5.95% (employee), basic exemption $3,500, YMPE $74,600 (2026)
- **QPP contribution rates**: https://www.revenuquebec.ca/en/businesses/source-deductions-and-employer-contributions/quebec-pension-plan-qpp/contributions/
- **Key data (QPP)**: QPP rate 6.40% (employee, 2026), QPP max benefit at 65 $17,334 (2026)
- **Used by**: tax_data.py (cpp_rate, qpp_rate, cpp_exemption), cpp_sharing.py (compute_cpp2_contribution, CPP_BASIC_EXEMPTION), countries/canada/provinces/quebec/tax_data.py

## CPP/QPP Survivor Benefits
- **CPP survivor pension**: https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-survivor-benefit.html
- **QPP survivor pension**: https://www.retr.quebec.ca/en/actualites/regime-de-retraite-du-quebec
- **Key data (CPP)**: 60% of deceased's CPP for survivor 65+, 37.5% for under 65; combined cap at individual max
- **Key data (QPP)**: Flat-rate + 37.5% of deceased's QPP; combined cap at individual max
- **Used by**: cpp_sharing.py (compute_survivor_benefit, CPP_SURVIVOR_RATE_65_PLUS, QPP_SURVIVOR_RATE)

## RRIF Minimum Withdrawal Factors
- **CRA RRIF minimum withdrawal factors**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/registering-your-plan/minimum-withdrawal-factors-rrifs.html
- **Key data**: Age 71 mandatory conversion; minimum % increases from 4% (age 65) to ~5.82% (age 90+) per CRA prescribed factors
- **Used by**: retirement.py (RRIF_MIN_WITHDRAWAL_RATES), pension_split_optimizer.py (rrif_minimum_withdrawal)

## OAS Amounts and Clawback Thresholds (Year-Indexed)
- **OAS program page**: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security.html
- **OAS amounts by quarter**: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/oas-amounts.html
- **OAS recovery tax (clawback)**: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/recovery-tax.html
- **Key data (2026)**: OAS max $742.31/month (age 65-74), $816.52/month (75+); clawback threshold $95,312
- **Used by**: retirement.py (OAS_ANNUAL_MAX, OAS_CLAWBACK_THRESHOLD, CPP_OAS_BY_YEAR), pension_split_optimizer.py

## Rental Income and CCA (Capital Cost Allowance)
- **CRA T776 guide**: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4036/rental-income.html
- **CCA for rental property**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/rental-income/capital-cost-allowance-rental-property.html
- **Form T776**: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/rental-income/completing-form-t776-statement-real-estate-rentals.html
- **Key data**: Class 1 (4% CCA for buildings); Class 8 (20% for furniture/equipment); half-year rule; recapture on disposition
- **Used by**: rental.py (depreciation/CCA field in RentalExpenses)

## Federal and Provincial Basic Personal Amounts
- **Federal basic personal amount**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html
- **Quebec basic personal amount**: https://www.revenuquebec.ca/en/individuals/income-tax-rates/
- **Ontario basic personal amount**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/canadian-income-tax-rates-individuals-tax-years.html
- **Key data (2026)**: Federal $16,129; Quebec $17,383; Ontario $11,865
- **Used by**: tax_data.py (basic_personal_amount), countries/canada/provinces/quebec.py, countries/canada/provinces/ontario.py

## Provincial Dividend Tax Credit Rates
- **Quebec DTC rates**: https://www.revenuquebec.ca/en/individuals/income-tax-rates/ (see Provincial Income Tax Rates and Deductions)
- **Ontario DTC rates**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/eligible-dividends.html (provincial table)
- **CRA eligible dividends overview**: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/eligible-dividends.html
- **Key data (2026)**: QC eligible DTC 11.51% of grossed-up; QC non-eligible DTC 5.575%; ON varies
- **Used by**: income_type.py (QC_ELIGIBLE_DTC_RATE, QC_NON_ELIGIBLE_DTC_RATE)

## Prescribed Interest Rates (Loans)
- **CRA prescribed interest rates**: https://www.canada.ca/en/revenue-agency/services/tax/prescribed-interest-rates.html
- **Key data (2026 Q2)**: Prescribed rate for loans 3%; overdue tax rate 7%
- **ITA Section**: s.74.5(2), s.80.4(1)
- **Used by**: debt.py (PrescribedRateLoan), attribution.py (prescribed-rate loan exception)

---

*Last updated: 2026-06-07*
*When adding new programs, update this file and add references in the module docstring.*
## Alternative Minimum Tax (AMT)
- **ITA Section**: Part I, Division F (s.127.5–127.6)
- **CRA AMT guide (Form T691)**: https://www.canada.ca/en/revenue-agency/services/forms-publications/forms/t691.html
- **2024 Federal Budget AMT changes**: https://www.budget.canada.ca/2024/report-rapport/toc-tdm-en.html (Chapter 4: Fairness)
- **Key data (2024+)**: Exemption $173,000; rate 15%; exemption phases out at 25% for AMTI above $173,000; 7-year carryforward
- **Key data (pre-2024)**: Exemption ~$40,000 for individuals; rate 15%; no phaseout; 7-year carryforward
- **Used by**: amt.py (AMTParameters, compute_amt), rrsp_ledger.py (deduct_later_analysis AMT risk)

## Tuition Tax Credits
- **Federal tuition tax credit**: ITA s.118.5 (line 32000); credit = eligible tuition × lowest federal bracket rate (15% in 2026). CRA: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-completing-a-tax-return/deductions-credits-expenses/lines-32300-32400-32500-32600-32700-32701-32702-32703/tuition-amounts.html
- **Quebec provincial tuition credit (TP-1 Schedule T)**: non-refundable credit = eligible tuition × 8% (a SPECIFIC tuition rate, not the 14% general lowest-bracket rate; a 20% rate applies only to pre-2013 carryforwards). Revenu Québec Schedule T (TP-1.D.T-V): https://www.revenuquebec.ca/en/individuals/income-tax-return/completing-your-income-tax-return/lines-to-complete/schedule-t/  (line 45: "Multiply line 44.1 by 8%. Tax credit for tuition or examination fees").
- **Key data (2026)**: Federal 15% (lowest bracket); Quebec 8% (specific tuition credit rate).
- **Used by**: countries/canada/tax_calc.py (tuition_tax_credit, parameterized by province), tax_data.py (qc_tuition_credit_rate), countries/canada/provinces/quebec/tax_data.py
