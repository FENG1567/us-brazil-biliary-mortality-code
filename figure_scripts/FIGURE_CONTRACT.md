# Figure contract

Core conclusion: Broadening biliary mortality from underlying cause to available mentions lowers the US–Brazil relative rate ratio but widens the absolute rate difference; code-specific effects, certificate context, and a separate age-0–74 temporal comparison qualify that overall result.

Figure archetype: Quantitative grid.

Target/output: General original-research figures; final width 183 mm; white background; Arial text; editable SVG/PDF plus 600 dpi TIFF and PNG.

Backend: R only (ggplot2, patchwork, svglite, Cairo PDF, ragg).

Panel map:

- Figure 1a: Full-period overall standardized rates by country and definition; country color and shape set the visual vocabulary.
- Figure 1b: Brazil:US rate ratios under UCD and available-mention definitions, with nominal intervals.
- Figure 1c: Brazil-minus-US rate differences under each definition, on a separate scale with nominal intervals.
- Figure 2a: All eight fixed-code RRRs with nominal 95% and family-adjusted 99.444% intervals; directly annotates the K83.0-versus-K83.1 heterogeneity contrast.
- Figure 2b: US and Brazil available-mention/UCD expansion factors as points only; corresponding interval estimates are available but not shown.
- Figure 3a: Mutually exclusive signed partition of the overall DD_A into focus classes with malignant UCD, a biliary-family set containing K83, and biliary-family mentions only in Part I; other positive classes, negative classes, and net. The waterfall uses a neutral gray palette with signed value labels.
- Figure 3b-c: Full definition and two descriptive record-level restrictions shown as RRR and DD_A points: exclude deaths with malignant UCD, or restrict to records with no malignant code in any available certificate field.
- Figure 4a-b: Early versus late 0–74 Brazil:US rate ratios and Brazil-US rate differences calculated from country-specific equal-weighted mean annual standardized rates, with the existing family-adjusted relative and absolute interaction intervals. UCD is charcoal/solid/circle; any mention is medium-gray/dashed/square.

Evidence hierarchy: Figure 1 carries the primary two-scale comparison; Figure 2 establishes fixed-code heterogeneity; Figure 3 describes joint certificate context and restricted-record sensitivity; Figure 4 shows the limited temporal application using its separate age target.

Statistics: Direct standardization uses WHO 18 age weights, equal sex weights, and equal year weights for the 2012–2023 all-age target. The primary overall RRR and DD_A use individual Bonferroni-adjusted 97.5% percentile intervals, providing 95% family-wise coverage for the two-contrast family. Figure 1b-c country-specific rate ratios and differences show nominal 95% event-process percentile intervals; intervals for the standardized rate points were not computed. The fixed-code secondary family comprises eight RRRs plus one direct K83 heterogeneity contrast and uses 99.444% per-contrast intervals. Expansion-factor interval estimates are available in the full-axis source results but are not displayed in Figure 2b. Figure 3 M1 composition, descriptive record-restriction estimates, and Figure 4 early/late point estimates are shown without pointwise intervals. Figure 4 uses the existing reported seed and 99.1667% per-contrast intervals for its six-contrast time family.

Source data: Clean panel CSVs are delivered beside the figures; a SHA-256 map links each panel CSV to its upstream summary-input roles. The plotting script checks the formal points against the published summary tables and checks M1 closure against the primary DD_A.

Image integrity: No source images or raster observations are used. All panels are vector plots with R-rendered PNG/TIFF exports. No data rows are omitted for convenience; M1 zero-contribution classes remain in source data and are only visually suppressed as zero-length bars.

Reviewer risks: Do not interpret RRR < 1 as universally closer to equality; code groups overlap and are non-additive; confidence intervals for standardized rate points were not computed; Figure 2b expansion-factor intervals exist but are not shown; Figure 3 restrictions drop records according to malignant UCD or any malignant code among available fields rather than removing a mention source, and do not define cancer-free populations; Figure 4’s color, line type, and symbol encode definition (not country or period); the 0–74 target is distinct from the all-age primary target; interval scope is conditional event-process uncertainty and does not capture certification, coverage, or source-version uncertainty.

## Supplementary Figure S1 contract

Core conclusion: Annual all-age standardized biliary mortality rates from 2012–2023 are shown descriptively by country and definition.

Figure archetype: Quantitative grid (two country-specific panels).

Backend/output: R only; 183 mm wide with editable SVG/PDF and 600 dpi TIFF/PNG exports.

Panel map and encoding: a United States; b Brazil. Country is encoded by the established blue/orange color and circle/triangle point symbol. Within each country panel, solid UCD and dashed available-mention lines are identified by direct endpoint labels. Each panel has an independent, explicitly labeled y scale.

Data and selection: The annual summary contains 52 observations covering 2012–2024. Plot 48 observations for 2012–2023; exclude the four 2024 observations because they fall outside the primary version window. Preserve the unmodified source data and log the four excluded rows. Annual-rate confidence intervals were not computed.

Reviewer risk: These are descriptive annual points without rate intervals; the country panels have independent y scales, so vertical distances should not be compared across countries.
