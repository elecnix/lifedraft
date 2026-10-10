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

## Best Per Category

| Category | Net Benefit |
| --- | --- |
| Fill Registered Room Yes (Readvanceable) Yes (Stagger Years) | $8,831,033 |
| Fill Registered Room No No | $5,280,551 |

## Optimal Refinance Level

| Readvanceable Mortgage | Staggered Deduction | No Refinance | Fill Registered Room | Maximum Refinance (80%) | Best |
| --- | --- | --- | --- | --- | --- |
| Yes (Readvanceable) | Yes (Stagger Years) | $0 | $8,831,033 | $0 | Fill Registered Room |
| No | No | $0 | $5,280,551 | $0 | Fill Registered Room |

## Top 15 Scenarios

| # | Scenario | Loan-to-Value | Net Benefit | Liquid NW | Assets | Debt | Decumulation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | ? | 80.0% | $8,831,033 | $10,865,760 | $11,635,818 | $770,058 |  |
| 2 | ? | 80.0% | $8,822,850 | $10,986,541 | $11,756,599 | $770,058 |  |
| 3 | ? | 80.0% | $8,662,096 | $10,800,671 | $11,570,729 | $770,058 |  |
| 4 | ? | 80.0% | $8,368,582 | $11,116,136 | $11,886,194 | $770,058 |  |
| 5 | ? | 80.0% | $8,368,582 | $11,116,136 | $11,886,194 | $770,058 |  |
| 6 | ? | 80.0% | $8,227,007 | $10,227,833 | $10,997,891 | $770,058 |  |
| 7 | ? | 80.0% | $8,208,594 | $10,843,128 | $11,613,186 | $770,058 |  |
| 8 | ? | 80.0% | $8,201,315 | $10,317,448 | $11,087,506 | $770,058 |  |
| 9 | ? | 80.0% | $8,065,850 | $10,159,635 | $10,929,693 | $770,058 |  |
| 10 | ? | 80.0% | $7,816,556 | $9,721,604 | $10,843,946 | $1,122,342 |  |
| 11 | ? | 80.0% | $7,811,662 | $10,459,877 | $11,229,935 | $770,058 |  |
| 12 | ? | 80.0% | $7,811,662 | $10,459,877 | $11,229,935 | $770,058 |  |
| 13 | ? | 80.0% | $7,808,629 | $9,816,266 | $10,938,608 | $1,122,342 |  |
| 14 | ? | 80.0% | $7,681,176 | $10,238,683 | $11,008,741 | $770,058 |  |
| 15 | ? | 80.0% | $7,664,343 | $9,648,849 | $10,771,191 | $1,122,342 |  |

## Year-by-Year Breakdown — #1 Scenario: ?

### Balances

| Year | Total RRSP | RRSP (Primary) | Spousal RRSP | RRSP (Spouse) | Total TFSA | TFSA (Primary) | TFSA (Spouse) | RESP | Non-Reg | Non-Reg ACB | Unreal. Gains | CRI/LIRA | LIF | Total Assets | Total Debt | Net Worth |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | $463,754 | $266,818 | $63,528 | $133,409 | $103,586 | $19,026 | $84,560 | $28,718 | $163,449 | $132,134 | $31,315 | $40,280 | $0 | $905,862 | $529,642 | $376,219 |
| 2 | $514,507 | $290,795 | $70,026 | $153,686 | $120,523 | $25,627 | $94,896 | $32,239 | $190,036 | $149,095 | $40,941 | $42,697 | $0 | $997,458 | $526,588 | $470,870 |
| 3 | $561,627 | $313,847 | $76,128 | $171,652 | $135,316 | $31,049 | $104,267 | $35,465 | $213,007 | $161,276 | $51,731 | $45,259 | $0 | $1,097,014 | $523,322 | $573,692 |
| 4 | $608,374 | $337,145 | $82,219 | $189,011 | $149,477 | $36,043 | $113,434 | $38,643 | $234,813 | $171,188 | $63,625 | $47,974 | $0 | $1,205,108 | $520,000 | $685,108 |
| 5 | $657,943 | $361,838 | $88,677 | $207,428 | $164,478 | $41,338 | $123,140 | $42,018 | $257,837 | $181,152 | $76,685 | $50,853 | $0 | $1,325,733 | $520,000 | $805,733 |
| 6 | $710,989 | $388,009 | $95,522 | $227,457 | $180,369 | $46,952 | $133,417 | $33,404 | $283,282 | $192,248 | $91,034 | $53,904 | $0 | $1,445,426 | $520,000 | $925,426 |
| 7 | $758,846 | $412,887 | $101,827 | $244,132 | $193,396 | $51,001 | $142,395 | $23,606 | $303,322 | $196,924 | $106,398 | $57,138 | $0 | $1,555,884 | $520,000 | $1,035,884 |
| 8 | $816,827 | $442,113 | $107,814 | $266,900 | $211,006 | $57,202 | $153,805 | $12,511 | $332,547 | $209,231 | $123,316 | $60,566 | $0 | $1,692,327 | $520,000 | $1,172,327 |
| 9 | $878,292 | $473,085 | $114,154 | $291,053 | $229,657 | $63,774 | $165,883 | $0 | $363,411 | $221,607 | $141,804 | $64,200 | $0 | $1,837,099 | $520,000 | $1,317,099 |
| 10 | $943,446 | $505,907 | $120,866 | $316,673 | $249,408 | $70,739 | $178,669 | $0 | $396,003 | $234,053 | $161,950 | $68,052 | $0 | $2,004,734 | $520,000 | $1,484,734 |
| 11 | $1,012,509 | $540,687 | $127,973 | $343,849 | $270,323 | $78,121 | $192,203 | $0 | $430,416 | $246,569 | $183,847 | $72,135 | $0 | $2,183,366 | $520,000 | $1,663,366 |
| 12 | $1,085,711 | $577,541 | $135,498 | $372,673 | $292,469 | $85,942 | $206,527 | $0 | $466,751 | $259,159 | $207,592 | $76,463 | $0 | $2,373,677 | $520,000 | $1,853,677 |
| 13 | $1,163,299 | $616,592 | $143,465 | $403,242 | $315,917 | $94,230 | $221,688 | $0 | $505,111 | $271,822 | $233,289 | $81,051 | $0 | $2,576,393 | $520,000 | $2,056,393 |
| 14 | $1,245,530 | $657,969 | $151,901 | $435,660 | $340,742 | $103,010 | $237,733 | $0 | $545,940 | $284,561 | $261,379 | $85,914 | $0 | $2,792,938 | $520,000 | $2,272,938 |
| 15 | $1,323,032 | $698,238 | $160,832 | $463,962 | $362,267 | $109,933 | $252,335 | $0 | $564,765 | $272,371 | $292,394 | $91,069 | $0 | $2,970,611 | $550,000 | $2,420,611 |
| 16 | $1,405,176 | $740,905 | $170,289 | $493,982 | $385,061 | $117,271 | $267,790 | $0 | $537,320 | $212,846 | $324,474 | $96,533 | $0 | $3,111,072 | $535,249 | $2,575,823 |
| 17 | $1,487,800 | $784,470 | $180,302 | $523,028 | $407,009 | $123,955 | $283,054 | $0 | $422,892 | $68,111 | $354,781 | $102,325 | $0 | $3,145,867 | $498,199 | $2,647,667 |
| 18 | $1,575,283 | $830,597 | $190,904 | $553,782 | $430,209 | $131,021 | $299,188 | $0 | $297,179 | $0 | $297,179 | $108,465 | $0 | $3,178,032 | $459,509 | $2,718,522 |
| 19 | $1,667,910 | $879,436 | $202,129 | $586,344 | $454,731 | $138,489 | $316,242 | $0 | $210,529 | $0 | $210,529 | $114,973 | $0 | $3,258,417 | $462,193 | $2,796,225 |
| 20 | $1,765,983 | $931,147 | $214,014 | $620,821 | $480,650 | $146,383 | $334,268 | $0 | $144,269 | $0 | $144,269 | $121,871 | $0 | $3,368,881 | $465,070 | $2,903,811 |
| 21 | $1,869,823 | $985,899 | $226,599 | $657,325 | $508,048 | $154,727 | $353,321 | $0 | $74,272 | $0 | $74,272 | $129,183 | $0 | $3,485,858 | $468,157 | $3,017,702 |
| 22 | $1,979,768 | $1,043,869 | $239,923 | $695,976 | $537,006 | $163,546 | $373,460 | $0 | $25,072 | $0 | $25,072 | $136,934 | $0 | $3,634,479 | $471,467 | $3,163,012 |
| 23 | $2,064,798 | $1,073,868 | $254,030 | $736,900 | $567,487 | $172,829 | $394,658 | $0 | $0 | $0 | $0 | $145,150 | $0 | $3,787,194 | $475,017 | $3,312,177 |
| 24 | $2,123,763 | $1,074,567 | $268,967 | $780,229 | $599,706 | $182,641 | $417,064 | $0 | $0 | $0 | $0 | $153,860 | $0 | $3,944,206 | $478,824 | $3,465,381 |
| 25 | $2,186,196 | $1,075,307 | $284,782 | $826,107 | $633,760 | $193,013 | $440,748 | $0 | $0 | $0 | $0 | $163,091 | $0 | $4,110,275 | $482,908 | $3,627,367 |
| 26 | $2,257,968 | $1,081,758 | $301,527 | $874,682 | $665,544 | $202,692 | $462,851 | $0 | $0 | $0 | $0 | $172,877 | $0 | $4,287,381 | $487,288 | $3,800,093 |
| 27 | $2,332,321 | $1,086,951 | $319,257 | $926,113 | $700,357 | $213,295 | $487,062 | $0 | $0 | $0 | $0 | $183,249 | $0 | $4,474,292 | $491,985 | $3,982,308 |
| 28 | $2,343,598 | $1,090,755 | $338,029 | $914,813 | $740,277 | $225,453 | $514,825 | $0 | $46,998 | $46,998 | $0 | $0 | $183,249 | $4,643,672 | $497,022 | $4,146,649 |
| 29 | $2,351,902 | $1,093,046 | $357,906 | $900,951 | $782,473 | $238,303 | $544,170 | $0 | $99,349 | $96,698 | $2,651 | $0 | $183,988 | $4,822,472 | $502,425 | $4,320,047 |
| 30 | $2,356,964 | $1,093,702 | $378,950 | $884,312 | $827,074 | $251,887 | $575,187 | $0 | $157,423 | $149,169 | $8,254 | $0 | $184,496 | $5,010,185 | $508,220 | $4,501,965 |
| 31 | $2,358,523 | $1,092,608 | $401,233 | $864,682 | $874,217 | $266,244 | $607,973 | $0 | $221,597 | $204,464 | $17,134 | $0 | $184,751 | $5,207,280 | $514,434 | $4,692,845 |
| 32 | $2,356,333 | $1,089,658 | $424,825 | $841,849 | $924,047 | $281,420 | $642,627 | $0 | $292,245 | $262,612 | $29,633 | $0 | $184,732 | $5,414,263 | $521,100 | $4,893,163 |
| 33 | $2,350,162 | $1,084,754 | $449,805 | $815,603 | $976,718 | $297,461 | $679,257 | $0 | $369,435 | $323,319 | $46,116 | $0 | $184,419 | $5,631,374 | $528,248 | $5,103,126 |
| 34 | $2,339,803 | $1,077,812 | $476,254 | $785,738 | $1,032,391 | $314,416 | $717,975 | $0 | $453,503 | $386,550 | $66,954 | $0 | $183,795 | $5,859,169 | $535,915 | $5,323,254 |
| 35 | $2,325,071 | $1,068,758 | $504,257 | $752,055 | $1,091,237 | $332,338 | $758,899 | $0 | $544,793 | $452,260 | $92,533 | $0 | $182,841 | $6,098,259 | $544,137 | $5,554,122 |
| 36 | $2,305,808 | $1,057,536 | $533,908 | $714,364 | $1,153,438 | $351,281 | $802,157 | $0 | $643,638 | $520,378 | $123,261 | $0 | $181,543 | $6,349,307 | $552,956 | $5,796,351 |
| 37 | $2,281,786 | $1,044,000 | $565,301 | $672,485 | $1,219,184 | $371,304 | $847,879 | $0 | $750,447 | $590,882 | $159,564 | $0 | $179,889 | $6,613,003 | $562,414 | $6,050,589 |
| 38 | $2,252,921 | $1,028,131 | $598,541 | $626,249 | $1,288,677 | $392,469 | $896,209 | $0 | $865,531 | $663,639 | $201,892 | $0 | $177,868 | $6,890,123 | $572,558 | $6,317,565 |
| 39 | $2,219,148 | $1,010,036 | $633,735 | $575,377 | $1,362,132 | $414,840 | $947,292 | $0 | $989,190 | $738,479 | $250,711 | $0 | $175,474 | $7,181,481 | $583,437 | $6,598,044 |
| 40 | $2,180,367 | $989,633 | $670,999 | $519,735 | $1,439,774 | $438,485 | $1,001,288 | $0 | $1,121,786 | $815,282 | $306,504 | $0 | $172,685 | $7,487,941 | $595,105 | $6,892,836 |
| 41 | $2,136,748 | $966,971 | $710,454 | $459,323 | $1,521,841 | $463,479 | $1,058,362 | $0 | $1,263,523 | $893,746 | $369,777 | $0 | $169,501 | $7,810,529 | $607,619 | $7,202,910 |
| 42 | $2,088,170 | $942,023 | $752,228 | $393,919 | $1,608,586 | $489,897 | $1,118,688 | $0 | $1,414,800 | $973,756 | $441,044 | $0 | $165,944 | $8,150,243 | $621,040 | $7,529,203 |
| 43 | $2,034,793 | $914,893 | $796,459 | $323,441 | $1,700,275 | $517,821 | $1,182,453 | $0 | $1,575,854 | $1,055,010 | $520,844 | $0 | $162,004 | $8,508,201 | $635,434 | $7,872,767 |
| 44 | $1,976,623 | $885,616 | $843,291 | $247,716 | $1,797,191 | $547,337 | $1,249,853 | $0 | $1,747,039 | $1,137,312 | $609,727 | $0 | $157,695 | $8,885,551 | $650,872 | $8,234,679 |
| 45 | $1,914,029 | $854,442 | $892,877 | $166,710 | $1,899,630 | $578,536 | $1,321,095 | $0 | $1,928,488 | $1,220,222 | $708,266 | $0 | $153,015 | $9,283,612 | $667,429 | $8,616,183 |
| 46 | $1,847,140 | $821,461 | $945,378 | $80,302 | $2,007,909 | $611,512 | $1,396,397 | $0 | $2,120,517 | $1,303,477 | $817,039 | $0 | $147,988 | $9,703,718 | $685,186 | $9,018,531 |
| 47 | $1,776,453 | $786,877 | $989,576 | $0 | $2,122,360 | $646,368 | $1,475,992 | $0 | $2,323,236 | $1,386,592 | $936,644 | $0 | $142,623 | $10,147,400 | $704,231 | $9,443,168 |
| 48 | $1,702,138 | $750,760 | $951,378 | $0 | $2,243,335 | $683,211 | $1,560,123 | $0 | $2,536,993 | $1,469,310 | $1,067,682 | $0 | $136,970 | $10,616,188 | $724,657 | $9,891,531 |
| 49 | $1,624,622 | $713,297 | $911,325 | $0 | $2,371,205 | $722,154 | $1,649,051 | $0 | $2,761,990 | $1,551,213 | $1,210,777 | $0 | $131,047 | $11,111,754 | $746,563 | $10,365,191 |
| 50 | $1,544,203 | $674,707 | $869,495 | $0 | $2,506,364 | $763,317 | $1,743,046 | $0 | $2,998,532 | $1,631,969 | $1,366,563 | $0 | $124,893 | $11,635,818 | $770,058 | $10,865,760 |

### Taxes & SM

| Year | Prim MTR | Spouse MTR | Bracket Gap | RRSP Tax Savings | SM Tax Savings | SM Interest | QC Deduct. Int | QC Carry-Fwd | SM Deduct % | SM Readvanced |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 45.71% | 36.12% | 9.59% | $15,779 | $244 | $1,046 | $3,099 | $0 | 50.95% | $19,195 |
| 2 | 47.46% | 36.12% | 11.34% | $18,679 | $550 | $2,140 | $3,627 | $0 | 54.18% | $20,077 |
| 3 | 47.46% | 36.12% | 11.34% | $11,739 | $884 | $3,285 | $4,227 | $0 | 56.69% | $21,000 |
| 4 | 47.46% | 36.12% | 11.34% | $5,374 | $1,251 | $4,482 | $4,893 | $0 | 58.81% | $21,964 |
| 5 | 47.46% | 36.12% | 11.34% | $5,384 | $1,642 | $5,734 | $5,615 | $0 | 60.53% | $22,973 |
| 6 | 47.46% | 36.12% | 11.34% | $5,529 | $2,025 | $7,043 | $6,398 | $0 | 62.06% | $24,029 |
| 7 | 47.46% | 36.12% | 11.34% | $2,313 | $2,455 | $8,413 | $7,275 | $0 | 63.84% | $25,133 |
| 8 | 45.71% | 36.12% | 9.59% | $5,006 | $2,936 | $9,846 | $8,198 | $0 | 65.23% | $26,287 |
| 9 | 45.71% | 36.12% | 9.59% | $5,034 | $3,445 | $11,344 | $9,178 | $0 | 66.44% | $27,495 |
| 10 | 45.71% | 36.12% | 9.59% | $5,062 | $3,908 | $12,912 | $10,217 | $0 | 67.52% | $28,758 |
| 11 | 45.71% | 36.12% | 9.59% | $5,091 | $4,317 | $14,551 | $11,317 | $0 | 68.48% | $30,079 |
| 12 | 45.71% | 36.12% | 9.59% | $5,019 | $4,745 | $16,266 | $12,479 | $0 | 69.35% | $31,461 |
| 13 | 45.71% | 36.12% | 9.59% | $4,930 | $5,049 | $18,059 | $13,706 | $0 | 70.16% | $32,906 |
| 14 | 41.12% | 36.12% | 5.00% | $4,960 | $5,455 | $19,935 | $15,001 | $0 | 70.90% | $34,418 |
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
| Primary laid off, EI then re-employed at the 12-month non-compete expiry (#767) | >=160 mo (survives working life) | ~0 mo | — |
| Stay at current jobs | n/a (no shock) | — | leans on an unsecured credit line (lender can cut it) |

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
