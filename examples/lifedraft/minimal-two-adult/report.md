# Strategy Optimizer Results

## Situation Summary

| Metric | Value |
| --- | --- |
| Primary Income | $118,000/yr |
| Spouse Income | $96,000/yr |
| House Value | $650,000 |
| Current Mortgage | $340,000 |
| Current Loan-to-Value | 52.3% |
| Margin Available | $150,000 |
| Refinance Cash-Out (80%) | $180,000 |
| Registered Room | $117,000 |
| Min Loan-to-Value to Fill | 52.3% |
| RESP Balance | $46,000 |

## Model Fidelity

As of 2026 | CAD | nominal dollars
Known approximations affecting this run's headline figures:
- net_benefit prices the RRSP / RRIF / spousal RRSP / LIF / LIRA balances as a deemed disposition AT THE PROJECTION HORIZON, via the same estate path as max_after_tax_estate: it treats the horizon as the date of death, so it does not model a pre-death drawdown that would spread that income over lower-bracket years -> net_benefit ranking figure (the default objective, and the console headline) [unknown] (#290)
- net_benefit prices the registered balances (issue #290) and the SM sleeve (issue #1034) via the estate code path, so the spousal-rollover election MOVES it; but it still prices the non-reg pot with its own marginal_rate (not the estate's progressive stacking + rollover), and it does not price TFSA / principal-residence / life-insurance at death at all -- so those estate elections remain inert; rank on max_after_tax_estate to see the FULL estate priced -> net_benefit ranking figure (the default objective, and the console headline) -- its partial blindness to the /estate election levers specifically [unknown] (#672)
- #1034 closed the SM sleeve's UNTAXED terminal gain but left a residual pro-leverage bias: net_benefit prices the SM sleeve's deemed disposition via the estate path (split across two terminal returns, progressive terminal-YEAR indexed brackets) but the non-reg pot via its own flat marginal_rate on ONE household return against START-year brackets -- so an IDENTICAL dollar of accrued gain is worth roughly 5% more inside the SM sleeve than outside it (measured: a $700k/$500k pot scores ~$674k as the sleeve vs ~$664k as non-reg, a ~$10k gap on a $200k gain). The two pots use different bases, not one; the inconsistency is not conservatively biased -- it favours leverage -> net_benefit ranking figure (the default objective) -- the residual cross-pot basis inconsistency that still tilts it toward leverage [unknown] (#1034)
- Declared RRSP contributions exceeded the contributor's room and the engine REFUSED the excess: the money booked $0 -- it entered no account, spilled nowhere, and stayed in no cash line. The output shows a plan whose contributions were not all made -> any plan or ranking that assumes the declared RRSP contributions were made; the household's real-world contribution would either sit unsheltered or trigger the CRA's 1%-per-month excess-contribution tax (T1-OVP) [unknown] (#170)
  - first refused contribution in year 1
  - $16,852 of declared OWN RRSP contributions were refused (above the contributor's room)
  - $0 of declared SPOUSAL RRSP contributions were refused (the contributor's pool was exhausted)
  - $4,045 of contributions declared to the SPOUSE'S OWN RRSP were refused (above the spouse's own room)
  - the refused amounts were NOT redirected -- they entered no account; a plan reading these contributions as made is wrong
- An RRSP contribution exceeded the deduction that could still reduce that year's tax, so the excess was carried forward undeducted and is claimed in later years at a PROJECTED income and rate -- not refunded in the contribution year -> rrsp_tax_savings (the RRSP refund) in the years after the carry, and any ranking that sums it [unknown] (#286)
  - year 15: RRSP contributions exceeded the deduction still useful against that year's taxable income; $4,973 was carried forward undeducted (not refunded that year)
  - largest year-end carry-forward: $10,045
- RRSP contributions already made but not yet deducted were not declared, so the deduction ledger starts empty: any carried-forward deduction on the Notice of Assessment is missing from the projection -> rrsp_tax_savings (the RRSP refund) in the early years [understates] (#286)
  - primary: RRSP room is declared but undeducted contributions are not -- the deduction ledger started empty. Declare room.rrsp.undeducted_contributions (Notice of Assessment > RRSP deduction limit statement > unused RRSP contributions available to deduct); 0 is a valid answer
  - spouse: RRSP room is declared but undeducted contributions are not -- the deduction ledger started empty. Declare room.rrsp.undeducted_contributions (Notice of Assessment > RRSP deduction limit statement > unused RRSP contributions available to deduct); 0 is a valid answer
- The RRSP refund is the bracket tax the deduction removes from the contributor's taxable income, before non-refundable credits (basic personal amount, etc.): at low income, where credits already bring the tax to zero, the modelled refund is too high -> rrsp_tax_savings (the RRSP refund) for a low-income contributor [overstates] (#286)
- New RESP contributions are split evenly across all children, including a child past the grant window, instead of being routed to the child who can still earn grants -> RESP balance, CESG/QESI paid, and terminal net worth [unknown] (#295)
- A family-plan RESP's balance is attributed to its beneficiaries in proportion to each one's contributions and grants -> per-child RESP balance, EAP timing and AIP tax at collapse [unknown] (#295)
- QESI is paid on each year's contribution only: unused QESI rights are not carried forward, and the 16-17 contribution condition is not applied to QESI -> QESI paid, RESP balance, and terminal net worth [unknown] (#295)

## Best Per Category

| Category | Net Benefit |
| --- | --- |
| Fill Registered Room Yes (Readvanceable) Yes (Stagger Years) | $9,208,661 |
| Fill Registered Room No No | $5,663,528 |

## Optimal Refinance Level

| Readvanceable Mortgage | Staggered Deduction | No Refinance | Fill Registered Room | Maximum Refinance (80%) | Best |
| --- | --- | --- | --- | --- | --- |
| Yes (Readvanceable) | Yes (Stagger Years) | $0 | $9,208,661 | $0 | Fill Registered Room |
| No | No | $0 | $5,663,528 | $0 | Fill Registered Room |

## Top 15 Scenarios

| # | Scenario | Loan-to-Value | Net Benefit | Liquid NW | Assets | Debt | Decumulation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | ? | 80.0% | $9,208,661 | $11,477,113 | $12,223,676 | $746,563 |  |
| 2 | ? | 80.0% | $9,207,002 | $11,331,482 | $12,078,045 | $746,563 |  |
| 3 | ? | 80.0% | $9,047,331 | $11,290,517 | $12,037,081 | $746,563 |  |
| 4 | ? | 80.0% | $8,750,337 | $11,612,468 | $12,359,031 | $746,563 |  |
| 5 | ? | 80.0% | $8,750,337 | $11,612,468 | $12,359,031 | $746,563 |  |
| 6 | ? | 80.0% | $8,704,044 | $10,734,159 | $11,496,731 | $762,572 |  |
| 7 | ? | 80.0% | $8,680,591 | $10,840,329 | $11,602,902 | $762,572 |  |
| 8 | ? | 80.0% | $8,558,429 | $11,311,426 | $12,057,989 | $746,563 |  |
| 9 | ? | 80.0% | $8,535,310 | $10,671,666 | $11,434,238 | $762,572 |  |
| 10 | ? | 80.0% | $8,503,232 | $10,561,943 | $11,308,506 | $746,563 |  |
| 11 | ? | 80.0% | $8,471,987 | $10,657,450 | $11,404,013 | $746,563 |  |
| 12 | ? | 80.0% | $8,336,049 | $10,499,043 | $11,245,606 | $746,563 |  |
| 13 | ? | 80.0% | $8,253,068 | $10,983,754 | $11,746,327 | $762,572 |  |
| 14 | ? | 80.0% | $8,253,068 | $10,983,754 | $11,746,327 | $762,572 |  |
| 15 | ? | 80.0% | $8,089,561 | $10,719,706 | $11,482,278 | $762,572 |  |

## Year-by-Year Breakdown — #1 Scenario: ?

### Balances

| Year | Total RRSP | RRSP (Primary) | Spousal RRSP | RRSP (Spouse) | Total TFSA | TFSA (Primary) | TFSA (Spouse) | RESP | Non-Reg | Non-Reg ACB | Unreal. Gains | CRI/LIRA | LIF | Total Assets | Total Debt | Net Worth |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | $463,754 | $266,818 | $63,528 | $133,409 | $103,586 | $19,026 | $84,560 | $22,802 | $163,449 | $132,134 | $31,315 | $40,280 | $0 | $899,945 | $529,642 | $370,303 |
| 2 | $514,507 | $290,795 | $70,026 | $153,686 | $120,523 | $25,627 | $94,896 | $25,968 | $190,036 | $149,095 | $40,941 | $42,697 | $0 | $1,001,706 | $526,588 | $475,118 |
| 3 | $561,627 | $313,847 | $76,128 | $171,652 | $135,316 | $31,049 | $104,267 | $28,817 | $213,007 | $161,276 | $51,731 | $45,259 | $0 | $1,111,938 | $523,322 | $588,615 |
| 4 | $608,374 | $337,145 | $82,219 | $189,011 | $149,477 | $36,043 | $113,434 | $31,597 | $234,813 | $171,188 | $63,625 | $47,974 | $0 | $1,229,110 | $520,000 | $709,110 |
| 5 | $657,943 | $361,838 | $88,677 | $207,428 | $164,478 | $41,338 | $123,140 | $34,549 | $257,837 | $181,152 | $76,685 | $50,853 | $0 | $1,354,079 | $520,000 | $834,079 |
| 6 | $710,989 | $388,009 | $95,522 | $227,457 | $180,369 | $46,952 | $133,417 | $27,466 | $283,282 | $192,248 | $91,034 | $53,904 | $0 | $1,478,741 | $520,000 | $958,741 |
| 7 | $758,846 | $412,887 | $101,827 | $244,132 | $193,396 | $51,001 | $142,395 | $19,410 | $303,322 | $196,924 | $106,398 | $57,138 | $0 | $1,592,119 | $520,000 | $1,072,119 |
| 8 | $816,827 | $442,113 | $107,814 | $266,900 | $211,006 | $57,202 | $153,805 | $10,287 | $332,547 | $209,231 | $123,316 | $60,566 | $0 | $1,731,747 | $520,000 | $1,211,747 |
| 9 | $878,292 | $473,085 | $114,154 | $291,053 | $229,657 | $63,774 | $165,883 | $0 | $363,411 | $221,607 | $141,804 | $64,200 | $0 | $1,879,992 | $520,000 | $1,359,992 |
| 10 | $943,446 | $505,907 | $120,866 | $316,673 | $249,408 | $70,739 | $178,669 | $0 | $396,003 | $234,053 | $161,950 | $68,052 | $0 | $2,048,914 | $520,000 | $1,528,914 |
| 11 | $1,012,509 | $540,687 | $127,973 | $343,849 | $270,323 | $78,121 | $192,203 | $0 | $430,416 | $246,569 | $183,847 | $72,135 | $0 | $2,228,871 | $520,000 | $1,708,871 |
| 12 | $1,085,711 | $577,541 | $135,498 | $372,673 | $292,469 | $85,942 | $206,527 | $0 | $466,751 | $259,159 | $207,592 | $76,463 | $0 | $2,420,548 | $520,000 | $1,900,548 |
| 13 | $1,163,299 | $616,592 | $143,465 | $403,242 | $315,917 | $94,230 | $221,688 | $0 | $505,111 | $271,822 | $233,289 | $81,051 | $0 | $2,624,669 | $520,000 | $2,104,669 |
| 14 | $1,245,530 | $657,969 | $151,901 | $435,660 | $340,742 | $103,010 | $237,733 | $0 | $545,940 | $284,561 | $261,379 | $85,914 | $0 | $2,842,663 | $520,000 | $2,322,663 |
| 15 | $1,321,276 | $696,482 | $160,832 | $463,962 | $362,267 | $109,933 | $252,335 | $0 | $580,883 | $288,489 | $292,394 | $91,069 | $0 | $2,999,346 | $520,000 | $2,479,346 |
| 16 | $1,403,317 | $739,046 | $170,289 | $493,982 | $385,061 | $117,271 | $267,790 | $0 | $610,099 | $284,716 | $325,383 | $96,533 | $0 | $3,181,992 | $533,074 | $2,648,918 |
| 17 | $1,364,718 | $707,898 | $133,792 | $523,028 | $407,009 | $123,955 | $283,054 | $0 | $598,910 | $239,115 | $359,795 | $102,325 | $0 | $3,198,802 | $495,867 | $2,702,935 |
| 18 | $1,323,849 | $673,426 | $96,641 | $553,782 | $430,209 | $131,021 | $299,188 | $0 | $586,683 | $193,107 | $393,576 | $108,465 | $0 | $3,216,101 | $457,008 | $2,759,094 |
| 19 | $1,280,577 | $635,405 | $58,827 | $586,344 | $454,731 | $138,489 | $316,242 | $0 | $619,633 | $192,967 | $426,666 | $114,973 | $0 | $3,280,188 | $459,509 | $2,820,679 |
| 20 | $1,264,407 | $602,505 | $41,081 | $620,821 | $480,650 | $146,383 | $334,268 | $0 | $654,441 | $192,825 | $461,616 | $121,871 | $0 | $3,377,477 | $462,193 | $2,915,284 |
| 21 | $1,247,285 | $566,086 | $23,874 | $657,325 | $508,048 | $154,727 | $353,321 | $0 | $691,212 | $192,684 | $498,529 | $129,183 | $0 | $3,480,262 | $465,070 | $3,015,192 |
| 22 | $1,258,181 | $536,927 | $25,277 | $695,976 | $537,006 | $163,546 | $373,460 | $0 | $730,057 | $192,542 | $537,515 | $136,934 | $0 | $3,617,878 | $468,157 | $3,149,721 |
| 23 | $1,269,717 | $506,054 | $26,764 | $736,900 | $567,616 | $172,868 | $394,747 | $0 | $771,093 | $192,400 | $578,693 | $145,150 | $0 | $3,763,335 | $471,467 | $3,291,868 |
| 24 | $1,281,932 | $473,365 | $28,337 | $780,229 | $599,970 | $182,722 | $417,248 | $0 | $814,443 | $192,257 | $622,185 | $153,860 | $0 | $3,917,081 | $475,017 | $3,442,064 |
| 25 | $1,294,865 | $438,754 | $30,004 | $826,107 | $634,168 | $193,137 | $441,031 | $0 | $860,237 | $192,115 | $668,123 | $163,091 | $0 | $4,079,589 | $478,824 | $3,600,764 |
| 26 | $1,331,724 | $425,274 | $31,768 | $874,682 | $670,316 | $204,146 | $466,170 | $0 | $889,459 | $172,817 | $716,643 | $172,877 | $0 | $4,255,368 | $482,908 | $3,772,460 |
| 27 | $1,370,550 | $410,801 | $33,636 | $926,113 | $708,524 | $215,782 | $492,742 | $0 | $920,421 | $153,610 | $766,811 | $183,249 | $0 | $4,441,108 | $487,288 | $3,953,821 |
| 28 | $1,377,746 | $412,238 | $35,614 | $929,894 | $748,909 | $228,082 | $520,828 | $0 | $980,327 | $161,601 | $818,726 | $0 | $183,249 | $4,619,781 | $491,985 | $4,127,796 |
| 29 | $1,383,246 | $413,104 | $37,708 | $932,434 | $791,597 | $241,082 | $550,515 | $0 | $1,045,186 | $171,166 | $874,020 | $0 | $183,988 | $4,808,779 | $497,022 | $4,311,756 |
| 30 | $1,386,889 | $413,352 | $39,925 | $933,612 | $836,718 | $254,824 | $581,894 | $0 | $1,115,324 | $182,352 | $932,972 | $0 | $184,496 | $5,007,656 | $502,425 | $4,505,231 |
| 31 | $1,388,520 | $412,939 | $42,273 | $933,309 | $884,411 | $269,349 | $615,062 | $0 | $1,191,073 | $195,193 | $995,880 | $0 | $184,751 | $5,216,947 | $508,220 | $4,708,727 |
| 32 | $1,387,991 | $411,824 | $44,758 | $931,409 | $934,823 | $284,702 | $650,121 | $0 | $1,272,771 | $209,710 | $1,063,061 | $0 | $184,732 | $5,437,222 | $514,434 | $4,922,788 |
| 33 | $1,385,162 | $409,970 | $47,390 | $927,801 | $988,108 | $300,930 | $687,178 | $0 | $1,360,763 | $225,913 | $1,134,849 | $0 | $184,419 | $5,669,090 | $521,100 | $5,147,991 |
| 34 | $1,379,905 | $407,347 | $50,177 | $922,382 | $1,044,430 | $318,083 | $726,347 | $0 | $1,455,398 | $243,797 | $1,211,601 | $0 | $183,795 | $5,913,203 | $528,248 | $5,384,955 |
| 35 | $1,372,106 | $403,925 | $53,127 | $915,055 | $1,103,962 | $336,213 | $767,749 | $0 | $1,557,029 | $263,339 | $1,293,690 | $0 | $182,841 | $6,170,256 | $535,915 | $5,634,341 |
| 36 | $1,361,669 | $399,684 | $56,251 | $905,734 | $1,166,888 | $355,378 | $811,510 | $0 | $1,666,014 | $284,502 | $1,381,512 | $0 | $181,543 | $6,440,993 | $544,137 | $5,896,856 |
| 37 | $1,348,472 | $394,568 | $59,558 | $894,346 | $1,233,401 | $375,634 | $857,766 | $0 | $1,782,740 | $307,259 | $1,475,481 | $0 | $179,889 | $6,726,199 | $552,956 | $6,173,243 |
| 38 | $1,332,459 | $388,570 | $63,060 | $880,828 | $1,303,704 | $397,045 | $906,659 | $0 | $1,907,566 | $331,533 | $1,576,034 | $0 | $177,868 | $7,026,723 | $562,414 | $6,464,309 |
| 39 | $1,313,538 | $381,731 | $66,768 | $865,038 | $1,378,016 | $419,677 | $958,339 | $0 | $2,040,894 | $357,267 | $1,683,627 | $0 | $175,474 | $7,343,459 | $572,558 | $6,770,901 |
| 40 | $1,291,664 | $374,021 | $70,694 | $846,949 | $1,456,563 | $443,598 | $1,012,964 | $0 | $2,183,110 | $384,370 | $1,798,740 | $0 | $172,685 | $7,677,350 | $583,437 | $7,093,912 |
| 41 | $1,266,948 | $365,455 | $74,851 | $826,642 | $1,539,587 | $468,884 | $1,070,703 | $0 | $2,334,503 | $412,628 | $1,921,875 | $0 | $169,501 | $8,029,455 | $595,105 | $7,434,350 |
| 42 | $1,239,309 | $356,027 | $79,252 | $804,030 | $1,627,343 | $495,610 | $1,131,733 | $0 | $2,495,527 | $441,978 | $2,053,549 | $0 | $165,944 | $8,400,867 | $607,619 | $7,793,248 |
| 43 | $1,208,829 | $345,773 | $83,912 | $779,143 | $1,720,102 | $523,860 | $1,196,242 | $0 | $2,666,538 | $472,233 | $2,194,305 | $0 | $162,004 | $8,792,747 | $621,040 | $8,171,707 |
| 44 | $1,175,497 | $334,708 | $88,846 | $751,942 | $1,818,147 | $553,720 | $1,264,428 | $0 | $2,847,981 | $503,274 | $2,344,707 | $0 | $157,695 | $9,206,324 | $635,434 | $8,570,889 |
| 45 | $1,139,501 | $322,927 | $94,071 | $722,503 | $1,921,782 | $585,282 | $1,336,500 | $0 | $3,040,182 | $534,839 | $2,505,343 | $0 | $153,015 | $9,642,929 | $650,872 | $8,992,057 |
| 46 | $1,100,905 | $310,462 | $99,602 | $690,842 | $2,031,323 | $618,643 | $1,412,680 | $0 | $3,243,517 | $566,697 | $2,676,819 | $0 | $147,988 | $10,103,897 | $667,429 | $9,436,468 |
| 47 | $1,060,011 | $297,391 | $105,459 | $657,161 | $2,147,109 | $653,905 | $1,493,203 | $0 | $3,458,316 | $598,551 | $2,859,765 | $0 | $142,623 | $10,590,786 | $685,186 | $9,905,599 |
| 48 | $1,016,924 | $283,741 | $111,659 | $621,523 | $2,269,494 | $691,178 | $1,578,316 | $0 | $3,685,077 | $630,251 | $3,054,825 | $0 | $136,970 | $11,105,217 | $704,231 | $10,400,986 |
| 49 | $971,898 | $269,582 | $118,225 | $584,091 | $2,398,855 | $730,575 | $1,668,280 | $0 | $3,924,236 | $661,560 | $3,262,676 | $0 | $131,047 | $11,648,927 | $724,657 | $10,924,270 |
| 50 | $925,077 | $254,998 | $125,177 | $544,903 | $2,535,590 | $772,218 | $1,763,372 | $0 | $4,176,289 | $692,272 | $3,484,017 | $0 | $124,893 | $12,223,676 | $746,563 | $11,477,113 |

### Taxes & SM

| Year | Prim MTR | Spouse MTR | Bracket Gap | RRSP Tax Savings | SM Tax Savings | SM Interest | QC Deduct. Int | QC Carry-Fwd | SM Deduct % | SM Readvanced |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 45.71% | 36.12% | 9.59% | $16,294 | $244 | $1,046 | $3,099 | $0 | 50.95% | $19,195 |
| 2 | 47.46% | 36.12% | 11.34% | $19,225 | $550 | $2,140 | $3,627 | $0 | 54.18% | $20,077 |
| 3 | 47.46% | 36.12% | 11.34% | $10,718 | $884 | $3,285 | $4,227 | $0 | 56.69% | $21,000 |
| 4 | 47.46% | 36.12% | 11.34% | $5,374 | $1,251 | $4,482 | $4,893 | $0 | 58.81% | $21,964 |
| 5 | 47.46% | 36.12% | 11.34% | $5,402 | $1,647 | $5,734 | $5,615 | $0 | 60.53% | $22,973 |
| 6 | 47.46% | 36.12% | 11.34% | $5,551 | $2,040 | $7,043 | $6,398 | $0 | 62.06% | $24,029 |
| 7 | 47.46% | 36.12% | 11.34% | $2,323 | $2,462 | $8,413 | $7,275 | $0 | 63.84% | $25,133 |
| 8 | 45.71% | 36.12% | 9.59% | $5,006 | $2,936 | $9,846 | $8,198 | $0 | 65.23% | $26,287 |
| 9 | 45.71% | 36.12% | 9.59% | $5,034 | $3,445 | $11,344 | $9,178 | $0 | 66.44% | $27,495 |
| 10 | 45.71% | 36.12% | 9.59% | $5,062 | $3,961 | $12,912 | $10,217 | $0 | 67.52% | $28,758 |
| 11 | 45.71% | 36.12% | 9.59% | $5,091 | $4,373 | $14,551 | $11,317 | $0 | 68.48% | $30,079 |
| 12 | 45.71% | 36.12% | 9.59% | $5,084 | $4,803 | $16,266 | $12,479 | $0 | 69.35% | $31,461 |
| 13 | 45.71% | 36.12% | 9.59% | $4,979 | $5,160 | $18,059 | $13,706 | $0 | 70.16% | $32,906 |
| 14 | 41.12% | 36.12% | 5.00% | $4,960 | $5,525 | $19,935 | $15,001 | $0 | 70.90% | $34,418 |
| 15 | 25.69% | 36.12% | -10.43% | $916 | $0 | $21,897 | $16,446 | $0 | 71.95% | $35,999 |
| 16 | 25.69% | 36.12% | -10.43% | $934 | $0 | $23,026 | $17,186 | $0 | 72.43% | $20,727 |
| 17 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,993 | $0 | 72.43% | $0 |
| 18 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,791 | $0 | 72.43% | $0 |
| 19 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $15,479 | $1,199 | 72.43% | $0 |
| 20 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,352 | $1,525 | 72.43% | $0 |
| 21 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $17,275 | $928 | 72.43% | $0 |
| 22 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $17,607 | $0 | 72.43% | $0 |
| 23 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 24 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 25 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 26 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 27 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 28 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 29 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 30 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 31 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 32 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 33 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 34 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 35 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 36 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 37 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 38 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 39 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 40 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 41 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 42 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 43 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 44 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 45 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 46 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 47 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 48 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 49 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |
| 50 | 25.69% | 25.69% | 0.00% | $0 | $0 | $23,026 | $16,678 | $0 | 72.43% | $0 |

## Runway — Months to Insolvency After the Income Shock

The headline is a *labelled interpolation* inside an honest bracket (the engine steps in years; ~N mo is a point estimate, [lo–hi] the range). `>=N mo (survives)` = the cushion outlasts the horizon.

| Scenario | Runway | Stress begins | Caveats |
| --- | --- | --- | --- |
| Primary promoted to director | >=160 mo (survives working life) | ~4 mo | — |
| Stay at current jobs | n/a (no shock) | — | — |
| Primary laid off, EI then re-employed at the 12-month non-compete expiry (#767) | >=160 mo (survives working life) | ~4 mo | — |

Runway UNDERSTATES reality: all spend is treated as rigid and contributions are counted as committed — a household in real distress stops both, so the true runway is longer. See the model-fidelity section.

## Per-Member Savings Plan

### Primary — income $118,000

| Account | Balance | Contribution Room |
| --- | --- | --- |
| RRSP | $210,000 | $42,000 |
| TFSA | $71,000 | $18,000 |

### Spouse — income $96,000

| Account | Balance | Contribution Room |
| --- | --- | --- |
| RRSP | $95,000 | $31,000 |
| TFSA | $54,000 | $26,000 |
| FHSA | $12,000 | $0 |

### child_a

_No registered accounts modelled._

### child_b

_No registered accounts modelled._

_Generated by lifedraft scenario enumerator — 24 scenarios evaluated._
