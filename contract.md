# Final analysis contract 20261025

Status: frozen before new full-window inference. This is post hoc finalization of already observed results, not a retrospective claim of original prespecification. Scientific and implementation gates were reviewed by the study team.

## Scientific objective and primary target

Estimate how underlying-cause versus available-mention definitions change US–Brazil biliary mortality comparisons on relative and absolute scales; characterize fixed code-specific contrasts and the joint certificate contexts of extra mentions, with a limited temporal monitoring application. No causal effect, clinical gold-standard validation, or correction of true mortality is claimed.

Use years 2012–2023 as the primary stable-version window because the exact Brazil 2024 final/provisional release identity is unknown. Retain 2012–2024 as explicit version-sensitive support. This is a source-version decision, not an effect-size decision. Use both windows already present in current retained inputs. Do not relabel old 18.4% and 52.7% findings as the new primary values.

Primary core estimand is direct standardization at all analytic ages: WHO18 age weights, female/male weights 0.5 each, year weights 1/12 or 1/13 respectively, fixed full population denominators. This makes main overall, code and M1 composition estimates share one target. Existing all-age regression estimates remain model sensitivity, not interchangeable primary numbers. Existing 0–74 time analysis retains its separate renormalized target and early 2012–2014/late 2021–2023 windows.

## Definitions and joint information

Use official retained parser output and release-selected source streams, not a directory-wide glob. Death record is the unit. U is group-specific UCD; A is union of UCD and released US entity-axis codes or Brazilian certificate-line codes. US structural alternative R is union of UCD and US record-axis codes. Keep the same U denominator for comparisons. Check raw definition nesting and source coverage rather than silently fixing conflicts. A death may belong to multiple fixed groups; never sum group results.

Fixed groups: K80, K81, K82, K83, K80–K81, K83.0, K83.1, K82.9. Include the total K80–K83 as the overall group. Normalize code tokens under the documented parser, no unbounded substring matches. Malignancy and certificate-position groups use the corrected M1 dictionary, including unknown categories and full population denominators after record restrictions.

For each target: RR_d=r_BR,d/r_US,d; RD_d=r_BR,d−r_US,d; RRR=RR_A/RR_U; DD=RD_A−RD_U, rates/RD/DD per 100000. E_c=r_c,A/r_c,U and RRR=E_BR/E_US. All manuscript quantities come from a unique result layer with estimand_id. RRR<1 is a numerical reduction in the signed country ratio, not universally movement toward equality.

## New inference and multiplicity

New calculations are needed for direct overall inference at the primary target (to avoid model/direct mixing), full-period fixed-code inference, direct K83.0 versus K83.1 heterogeneity, and code-specific US representation sensitivity. Reuse existing model and time inference if unchanged. M1 signed 240-class composition is descriptive, no 240-way significance search.

Construct joint count patterns by country/year/age/sex retaining each death's full U/A/R membership across all nine groups. Include every retained record relevant to any representation. Existing primary bridge patterns may be used as closure, but they omit record-axis masks. Read the small release-selected retained streams to create one sufficient joint aggregation for all endpoints and axes; do not infer cross-group covariance from marginal counts.

Event-process Poisson resampling conditional on fixed population and weights: independently draw Nstar[s,p] ~ Poisson(N[s,p]) for every disjoint stratum-pattern cell, then reuse that pattern count across all endpoints, groups and axes. Do NOT multiply the aggregated count by one Poisson(1) pattern weight, which has incorrect variance. The specified count draw is equivalent to summing independent Poisson(1) multipliers over deaths within that pattern. Validate this equivalence on a small synthetic example. State independence across countries and the conditional process interpretation; these intervals do not cover certification, coverage, missing-source or version uncertainty.

Two primary measures RRR and DD: nominal 95% intervals plus Bonferroni 97.5% family intervals. Secondary fixed-code family: eight full-period RRRs plus one direct difference H=log(RRR_K83.0)−log(RRR_K83.1), nine contrasts total, nominal 95% and Bonferroni 99.444444% intervals. Exp(H) is an interpretable ratio of RRRs. Avoid one-significant/one-not-significant reasoning. Do not run post hoc model/window/age search. Structural and 2024 analyses are descriptive sensitivity with nominal intervals and explicit exploratory status, not additional confirmatory claims.

Use seeds 20261025 and 20261026, 2000-draw diagnostic followed by fixed 50000 formal draws per seed to provide tail information for the secondary family. One job budget 8 total threads; chunk calculations to avoid excessive memory, save seed-specific draws or sufficient outputs and exact environment. No nested oversubscription. Compare key interval endpoints across seeds: absolute endpoint difference ≤5% of first interval width and stable interpretation. Investigate >1% invalid repetitions; do not silently discard or substitute zero. If precision at fixed maximum is inadequate, report Monte Carlo limitation and lower the claim rather than seek a different result.
Intervals use direct bootstrap percentile endpoints with NumPy quantile method='linear' at the fixed stated coverage; seed 20261025 supplies reported intervals and seed 20261026 supplies independent Monte Carlo stability checks. Do not choose whichever seed gives a stronger result. The two multiplicity families are separately protected, not one manuscript-wide 95% simultaneous guarantee. Historical Brazilian residence semantics remain a declared source limitation; the stable window is not complete source validation.

No continuity correction by default. Check zero-denominator/degenerate outcomes and identify non-estimable metrics. Group all-window counts are expected nonzero but this is verified, not assumed. Use unrounded inputs, numerical closure tolerance 1e−8 per 100000 for signed decomposition.

## Mandatory validation

Closure against current retained M2 points for both windows and strata; overall retained totals and population/weight closure; U⊆A/R under actual definitions; duplicated tokens do not increase death counts; cross-group overlap preserved; A=U implies RRR=1 and DD=0; RRR=E_BR/E_US; M1 categories partition and contributions sum to DD. Independent minimal recomputation/test review is required. Never reuse time-interaction intervals as full-period code intervals.

## Writing and external material boundary

Primary evidence chain: relative/absolute overall → complete fixed-code results → joint certificate context → distinct 0–74 time consequence. Colombia 2023 may be a bounded external sensitivity; unresolved nine fragments and source bounds are not CIs. CO2019/22 only current v4, and broad-format sensitivity is not silently omitted. Optional SIM/NHEFS/ZAF not automatically included; NHIS/ONS/internal QC stay archival unless a specific reader need warrants inclusion. SIH/SIA separate project. PARDoS/NHCS inquiry drafts only, UKB hold, no new applications/fees/messages without authority.

All original scientific information is mapped to retained text, appropriate supporting material, or an explicit justified exclusion. Do not use prose compression to delete independently informative methods. Preserve claim–citation alignment, factual author/funding statements and necessary limits; newly revised final author approval cannot be invented. WPS final actual-DOCX rendering and every-page inspection remain mandatory.
