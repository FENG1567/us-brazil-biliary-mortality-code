options(stringsAsFactors = FALSE, scipen = 6)
suppressWarnings(Sys.setlocale("LC_CTYPE", "Chinese (Simplified)_China.936"))
suppressPackageStartupMessages({ library(ggplot2); library(patchwork) })

trailing_args <- commandArgs(trailingOnly = TRUE)
root_index <- match("--project-root", trailing_args)
if (!is.na(root_index)) {
  if (root_index != 1L || length(trailing_args) != 2L) {
    stop("Usage: Rscript figure_scripts/create_figures.R [--project-root PATH]")
  }
  project_root <- normalizePath(trailing_args[[root_index + 1L]], winslash = "/", mustWork = TRUE)
} else {
  script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (!length(script_arg)) stop("Could not locate this script; supply --project-root PATH.")
  script_path <- normalizePath(sub("^--file=", "", script_arg[[1L]]), winslash = "/", mustWork = TRUE)
  project_root <- normalizePath(file.path(dirname(script_path), ".."), winslash = "/", mustWork = TRUE)
}
analysis_dir <- file.path(project_root, "02_analysis")
tables_dir <- file.path(project_root, "03_figures_tables", "source_data")
output_dir <- file.path(project_root, "03_figures_tables", "figures")
panel_dir <- file.path(output_dir, "source_data")
dir.create(panel_dir, recursive = TRUE, showWarnings = FALSE)

inputs <- list(
  formal_points = file.path(analysis_dir, "formal", "point_estimates.csv"),
  formal_intervals = file.path(analysis_dir, "formal", "intervals_seed20261025.csv"),
  table1 = file.path(tables_dir, "Table1_overall_two_scales.csv"),
  table2 = file.path(tables_dir, "Table2_fixed_eight_codes_two_scales.csv"),
  H = file.path(tables_dir, "Supplement_K83_H_and_expH.csv"),
  M1_focus = file.path(analysis_dir, "reused", "m1_m2_m3", "M1_focus_K83_PartI_malignant_DD.csv"),
  M1_240 = file.path(tables_dir, "Supplement_M1_joint_all_240.csv"),
  M3 = file.path(analysis_dir, "reused", "m1_m2_m3", "M3_age_under75_contrasts.csv"),
  time_points = file.path(analysis_dir, "reused", "time_0_74", "usbr_monitoring_point_estimates.csv"),
  time_intervals = file.path(tables_dir, "Supplement_time_0_74_six_family.csv")
)
if (any(!file.exists(unlist(inputs)))) stop("A required summary-data input is missing.")
read_input <- function(key) read.csv(inputs[[key]], check.names = FALSE, na.strings = c("", "NA"))
d <- lapply(names(inputs), read_input); names(d) <- names(inputs)
points <- d$formal_points
intervals <- d$formal_intervals
period <- "2012-2023"
total_group <- "K80-83_total"
codes <- c("K80", "K80-81", "K81", "K82", "K82.9", "K83", "K83.0", "K83.1")

overall_rows <- points[points[["window"]] == period & points[["case_definition"]] == "full" &
                         points[["code_group"]] == total_group & points[["axis_variant"]] == "entity_any", ]
if (nrow(overall_rows) != 1L) stop("Expected one formal overall row.")
overall <- overall_rows[1, ]
code_points <- points[points[["window"]] == period & points[["case_definition"]] == "full" &
                        points[["code_group"]] %in% codes & points[["axis_variant"]] == "entity_any", ]
code_points <- code_points[match(codes, code_points[["code_group"]]), ]
if (nrow(code_points) != 8L || anyNA(code_points[["code_group"]])) stop("Expected eight fixed-code rows.")

tab1_rrr <- d$table1[d$table1[["metric"]] == "RRR", ]
tab1_dd <- d$table1[d$table1[["metric"]] == "DD", ]
tab2_rrr <- d$table2[d$table2[["metric"]] == "RRR", ]
if (nrow(tab1_rrr) != 1L || nrow(tab1_dd) != 1L ||
    abs(overall[["RRR"]] - tab1_rrr[["estimate"]]) > 1e-10 ||
    abs(overall[["DD_A_per_100k"]] - tab1_dd[["estimate"]]) > 1e-10 ||
    nrow(tab2_rrr) != 8L ||
    max(abs(code_points[["RRR"]] - tab2_rrr[["estimate"]][match(codes, tab2_rrr[["code_group"]])])) > 1e-10) {
  stop("Formal point estimates do not match the publication summary tables.")
}

get_intervals <- function(metrics, groups = total_group, axes = NULL) {
  keep <- intervals[["window"]] == period & intervals[["code_group"]] %in% groups &
    intervals[["metric"]] %in% metrics &
    intervals[["seed_role"]] == "reported" & intervals[["phase"]] == "formal"
  if (!is.null(axes)) keep <- keep & intervals[["axis_variant"]] %in% axes
  intervals[keep, ]
}
main_ci <- get_intervals(c("RRR", "DD"), axes = "entity_any")
if (nrow(main_ci) != 2L ||
    any(main_ci[["family_id"]] != "primary_overall_RRR_DD") ||
    any(abs(main_ci[["adjusted_level"]] - 0.975) > 1e-10)) {
  stop("Primary intervals do not match the required family.")
}

theme_pub <- theme_classic(base_size = 7, base_family = "Arial") +
  theme(axis.line = element_line(linewidth = 0.32, colour = "black"),
        axis.ticks = element_line(linewidth = 0.32, colour = "black"),
        axis.title = element_text(size = 6.8, colour = "black"),
        axis.text = element_text(size = 6.2, colour = "black"),
        legend.title = element_text(size = 6.2, colour = "black"),
        legend.text = element_text(size = 6, colour = "black"),
        legend.key.width = grid::unit(4.5, "mm"),
        legend.key.height = grid::unit(3.5, "mm"),
        plot.title = element_text(size = 7.5, face = "bold", colour = "black", margin = margin(b = 2.5)),
        plot.subtitle = element_text(size = 6.1, colour = "#333333", margin = margin(b = 2.5)),
        plot.tag = element_text(size = 8, face = "bold", colour = "black"),
        plot.margin = margin(2.5, 2.5, 2, 2.5, unit = "mm"),
        panel.grid = element_blank())
theme_set(theme_pub)
pal <- c(US = "#2B6F9B", Brazil = "#C66B2B", UCD = "#2B6F9B", any_mention = "#C66B2B",
         focus = "#343A40", other_positive = "#B8BEC3", negative = "#737B82",
         net = "#20262B", restricted = "#737B82")
write_panel <- function(x, name) write.csv(x, file.path(panel_dir, name), row.names = FALSE, na = "", fileEncoding = "UTF-8")
save_pub <- function(plot, stem, height_mm, width_mm = 183, dpi = 600) {
  w <- width_mm / 25.4; h <- height_mm / 25.4
  svglite::svglite(file.path(output_dir, paste0(stem, ".svg")), width = w, height = h, bg = "white")
  print(plot); grDevices::dev.off()
  grDevices::cairo_pdf(file.path(output_dir, paste0(stem, ".pdf")), width = w, height = h, family = "Arial", bg = "white")
  print(plot); grDevices::dev.off()
  ragg::agg_tiff(file.path(output_dir, paste0(stem, ".tiff")), width = w, height = h, units = "in", res = dpi, background = "white", compression = "lzw")
  print(plot); grDevices::dev.off()
  ragg::agg_png(file.path(output_dir, paste0(stem, ".png")), width = w, height = h, units = "in", res = dpi, background = "white")
  print(plot); grDevices::dev.off()
}

# Figure 1
f1a <- data.frame(
  country = factor(c("US", "US", "Brazil", "Brazil"), levels = c("US", "Brazil")),
  definition = factor(c("UCD", "Any mention", "UCD", "Any mention"), levels = c("UCD", "Any mention")),
  deaths = c(overall[["n_US_U"]], overall[["n_US_A"]], overall[["n_BR_U"]], overall[["n_BR_A"]]),
  rate = c(overall[["r_US_U_per_100k"]], overall[["r_US_A_per_100k"]], overall[["r_BR_U_per_100k"]], overall[["r_BR_A_per_100k"]]),
  period = period, age_scope = "all_age"
)
f1b <- get_intervals(c("RR_U", "RR_A"))
f1b$definition <- factor(ifelse(f1b[["metric"]] == "RR_U", "UCD", "Any mention"), levels = c("UCD", "Any mention"))
f1b$estimate <- f1b[["point_estimate"]]; f1b$lower <- f1b[["nominal_lower"]]; f1b$upper <- f1b[["nominal_upper"]]
f1b <- f1b[, c("definition", "metric", "estimate", "lower", "upper", "nominal_level", "draws_requested", "draws_valid")]
f1b$y_pos <- as.numeric(f1b$definition)
f1c <- get_intervals(c("RD_U", "RD_A"))
f1c$definition <- factor(ifelse(f1c[["metric"]] == "RD_U", "UCD", "Any mention"), levels = c("UCD", "Any mention"))
f1c$estimate <- f1c[["point_estimate"]]; f1c$lower <- f1c[["nominal_lower"]]; f1c$upper <- f1c[["nominal_upper"]]
f1c <- f1c[, c("definition", "metric", "estimate", "lower", "upper", "nominal_level", "draws_requested", "draws_valid")]
f1c$y_pos <- as.numeric(f1c$definition)
if (nrow(f1b) != 2L || nrow(f1c) != 2L) stop("Figure 1 country-scale intervals are incomplete.")

p1a <- ggplot(f1a, aes(definition, rate, colour = country, shape = country, group = country)) +
  geom_line(linewidth = 0.55, alpha = 0.85) + geom_point(size = 2.2) +
  scale_colour_manual(values = pal[c("US", "Brazil")], name = "Country",
                      guide = guide_legend(override.aes = list(shape = c(16, 17)))) +
  scale_shape_manual(values = c(16, 17), guide = "none") +
  scale_x_discrete(labels = c("UCD", "Any\nmention")) +
  scale_y_continuous(limits = c(0, 4.65), breaks = 0:4, expand = expansion(mult = c(0, 0.02))) +
  labs(title = "Standardized mortality rates", x = NULL, y = "Rate per 100,000")
p1b <- ggplot(f1b, aes(x = estimate, y = y_pos, shape = definition)) +
  geom_vline(xintercept = 1, linetype = "dashed", linewidth = 0.35, colour = "#7A7A7A") +
  geom_segment(aes(x = lower, xend = upper, yend = y_pos), linewidth = 0.8, colour = "#4A4A4A") +
  geom_segment(aes(x = lower, xend = lower, y = y_pos - 0.025, yend = y_pos + 0.025), linewidth = 0.65, colour = "#4A4A4A") +
  geom_segment(aes(x = upper, xend = upper, y = y_pos - 0.025, yend = y_pos + 0.025), linewidth = 0.65, colour = "#4A4A4A") +
  geom_point(size = 1.45, colour = "#343A40") + scale_shape_manual(values = c(16, 17), guide = "none") +
  scale_x_continuous(limits = c(1, 3.35), breaks = c(1, 2, 3), expand = expansion(mult = c(0.01, 0.03))) +
  scale_y_continuous(breaks = 1:2, labels = c("UCD", "Any\nmention"), limits = c(0.5, 2.5), expand = expansion(add = 0)) +
  labs(title = "Relative scale", x = "Brazil:US rate ratio", y = NULL)
p1c <- ggplot(f1c, aes(x = estimate, y = y_pos, shape = definition)) +
  geom_vline(xintercept = 0, linetype = "dashed", linewidth = 0.35, colour = "#7A7A7A") +
  geom_segment(aes(x = lower, xend = upper, yend = y_pos), linewidth = 0.8, colour = "#4A4A4A") +
  geom_segment(aes(x = lower, xend = lower, y = y_pos - 0.025, yend = y_pos + 0.025), linewidth = 0.65, colour = "#4A4A4A") +
  geom_segment(aes(x = upper, xend = upper, y = y_pos - 0.025, yend = y_pos + 0.025), linewidth = 0.65, colour = "#4A4A4A") +
  geom_point(size = 1.45, colour = "#343A40") + scale_shape_manual(values = c(16, 17), guide = "none") +
  scale_x_continuous(limits = c(0, 2.9), breaks = c(0, 1, 2), expand = expansion(mult = c(0.01, 0.03))) +
  scale_y_continuous(breaks = 1:2, labels = c("UCD", "Any\nmention"), limits = c(0.5, 2.5), expand = expansion(add = 0)) +
  labs(title = "Absolute scale", x = "Brazil-US difference\nper 100,000", y = NULL)
fig1 <- (p1a | p1b | p1c) + plot_layout(widths = c(1.08, 1, 1.1)) + plot_annotation(tag_levels = "a")
fig1 <- fig1 & theme(legend.position = "bottom")
write_panel(f1a, "fig1a_overall_rates.csv")
write_panel(f1b[, c("definition","metric","estimate","lower","upper","nominal_level","draws_requested","draws_valid")], "fig1b_country_rate_ratios.csv")
write_panel(f1c[, c("definition","metric","estimate","lower","upper","nominal_level","draws_requested","draws_valid")], "fig1c_country_rate_differences.csv")
save_pub(fig1, "Figure1_overall_two_scales", 78)

# Figure 2
rrr_ci <- get_intervals("RRR", groups = codes, axes = "entity_any")
if (nrow(rrr_ci) != 8L) stop("Figure 2 must include all eight reported-seed RRR intervals.")
f2a <- data.frame(
  code_group = codes, estimate = code_points[["RRR"]],
  nominal_lower = rrr_ci[["nominal_lower"]][match(codes, rrr_ci[["code_group"]])],
  nominal_upper = rrr_ci[["nominal_upper"]][match(codes, rrr_ci[["code_group"]])],
  adjusted_lower = rrr_ci[["adjusted_lower"]][match(codes, rrr_ci[["code_group"]])],
  adjusted_upper = rrr_ci[["adjusted_upper"]][match(codes, rrr_ci[["code_group"]])],
  nominal_level = rrr_ci[["nominal_level"]][match(codes, rrr_ci[["code_group"]])],
  family_level = rrr_ci[["adjusted_level"]][match(codes, rrr_ci[["code_group"]])],
  family_size = rrr_ci[["family_size"]][match(codes, rrr_ci[["code_group"]])],
  draws_requested = rrr_ci[["draws_requested"]][match(codes, rrr_ci[["code_group"]])],
  draws_valid = rrr_ci[["draws_valid"]][match(codes, rrr_ci[["code_group"]])]
)
display_codes <- c("K80"="K80", "K80-81"="K80-K81", "K81"="K81", "K82"="K82", "K82.9"="K82.9", "K83"="K83", "K83.0"="K83.0", "K83.1"="K83.1")
f2a$label <- factor(unname(display_codes[f2a$code_group]), levels = rev(unname(display_codes[codes])))
f2b <- rbind(data.frame(code_group=codes, country="US", value=code_points[["E_US"]]),
             data.frame(code_group=codes, country="Brazil", value=code_points[["E_BR"]]))
f2b$label <- factor(unname(display_codes[f2b$code_group]), levels = rev(unname(display_codes[codes])))
f2b$country <- factor(f2b$country, levels = c("US", "Brazil"))
f2b$y <- 9 - match(f2b$code_group, codes) + ifelse(f2b$country == "US", -.13, .13)
h <- d$H[d$H[["window"]] == period & d$H[["axis_variant"]] == "entity_any", ]
if (nrow(h) != 1L || abs(h[["expH_RRR_ratio"]] - 3.020731) > 1e-5 ||
    abs(h[["expH_adjusted_ci_lower"]] - 2.830701) > 1e-5 ||
    abs(h[["expH_adjusted_ci_upper"]] - 3.228473) > 1e-5) stop("K83 heterogeneity annotation failed its value check.")
p2a <- ggplot(f2a, aes(y=label)) +
  geom_vline(xintercept=1, linetype="dashed", linewidth=.35, colour="#7A7A7A") +
  geom_segment(aes(x=adjusted_lower, xend=adjusted_upper, yend=label), linewidth=1.05, colour="#454B50") +
  geom_segment(aes(x=nominal_lower, xend=nominal_upper, yend=label), linewidth=.42, colour="#A9B0B5") +
  geom_point(aes(x=estimate), shape=21, size=1.35, stroke=.25, fill=pal[["UCD"]], colour="#202020") +
  scale_x_continuous(limits=c(.35,1.62), breaks=c(.5,.75,1,1.25,1.5), expand=expansion(mult=c(.01,.02))) +
  labs(title="Code-specific RRR", subtitle="K83.0 vs K83.1: exp(H) = 3.021 (99.444% family CI, 2.831-3.228)",
       x="RRR (ratio of Brazil:US rate ratios)", y=NULL)
p2b <- ggplot(f2b, aes(value, y, colour=country, shape=country)) +
  geom_vline(xintercept=1, linetype="dashed", linewidth=.35, colour="#7A7A7A") +
  geom_point(size=2) +
  scale_colour_manual(values=pal[c("US","Brazil")], name="Country") +
  scale_shape_manual(values=c(16,17), name="Country") +
  scale_y_continuous(breaks=1:8,labels=rev(unname(display_codes[codes])),limits=c(.5,8.5),expand=expansion(add=0)) +
  scale_x_continuous(limits=c(.5,7.2), breaks=1:7, expand=expansion(mult=c(.01,.02))) +
  labs(title="Expansion factors", subtitle="Points only; intervals not shown",
       x="Any-mention / UCD rate", y=NULL)
fig2 <- (p2a | p2b) + plot_layout(widths=c(1.3,1)) + plot_annotation(tag_levels="a")
fig2 <- fig2 & theme(legend.position="bottom")
write_panel(f2a[,c("code_group","estimate","nominal_lower","nominal_upper","adjusted_lower","adjusted_upper","nominal_level","family_level","family_size","draws_requested","draws_valid")],"fig2a_fixed_code_RRR.csv")
write_panel(f2b[,c("code_group","country","value")],"fig2b_expansion_factors.csv")
write_panel(h,"fig2_direct_K83_heterogeneity.csv")
save_pub(fig2,"Figure2_fixed_code_contrasts",102)

# Figure 3
m1 <- d$M1_240[d$M1_240[["period"]] == period, ]
if (nrow(m1) != 240L) stop("M1 source must contain 240 joint classes.")
m1_key <- paste(m1[["family_set"]], m1[["position_group"]], m1[["ucd_group"]], sep="||")
if (anyDuplicated(m1_key)) stop("M1 joint-class keys are not unique.")
focus_mask <- grepl("(^|\\|)K83(\\||$)",m1[["family_set"]]) &
  m1[["position_group"]] == "only_part_i" & m1[["ucd_group"]] == "malignant_C00_C97"
contrib <- m1[["DD_contribution_BR_minus_US_per_100k"]]
focus <- sum(contrib[focus_mask])
other_positive <- sum(contrib[!focus_mask & contrib > 0])
negative <- sum(contrib[!focus_mask & contrib < 0])
net <- sum(contrib)
zeros <- sum(!focus_mask & contrib == 0)
n_positive <- sum(!focus_mask & contrib > 0)
n_negative <- sum(!focus_mask & contrib < 0)
focus_check <- d$M1_focus[d$M1_focus[["period"]] == period &
                            d$M1_focus[["focus_definition"]] == "K83_in_family_set__only_PartI", ]
if (nrow(focus_check) != 1L ||
    abs(focus-focus_check[["DD_contribution_BR_minus_US_per_100k"]]) > 1e-8 ||
    abs(focus+other_positive+negative-net) > 1e-8 ||
    abs(net-overall[["DD_A_per_100k"]]) > 1e-8) stop("M1 partition does not close to the formal overall DD.")
before_negative <- focus + other_positive
f3a <- data.frame(
  component=c("Focus: malignant UCD;\nK83 included; Part I only","Other positive classes","Negative classes","Net DD"),
  component_key=c("focus","other_positive","negative","net"),
  value_per_100k=c(focus,other_positive,negative,net),
  bar_lower=c(0,focus,net,0), bar_upper=c(focus,before_negative,before_negative,net),
  n_classes=c(sum(focus_mask),n_positive,n_negative,nrow(m1)),
  zero_contribution_classes_omitted=zeros, period=period, age_scope="all_age"
)
f3a$component <- factor(f3a$component, levels=rev(f3a$component))
f3a$label_x <- f3a[["bar_upper"]] + .018
f3a$value_label <- ifelse(f3a[["value_per_100k"]] < 0,
                          sprintf("%.3f",f3a[["value_per_100k"]]),
                          sprintf("+%.3f",f3a[["value_per_100k"]]))

restriction_order <- c("full","exclude_malignant_UCD","no_malignancy_source_any")
restrictions <- d$M3[
  d$M3[["period"]] == period & d$M3[["age_scope"]] == "all_age" &
    d$M3[["code_group"]] == total_group & d$M3[["endpoint"]] == "DD_any_minus_UCD" &
    d$M3[["case_definition"]] %in% restriction_order, ]
restrictions <- restrictions[match(restriction_order,restrictions[["case_definition"]]),]
if (nrow(restrictions) != 3L || anyNA(restrictions[["case_definition"]]) ||
    abs(restrictions[["BR_over_US_rate_ratio"]][1]-overall[["RRR"]]) > 1e-8 ||
    abs(restrictions[["BR_minus_US_rate_difference_per_100k"]][1]-overall[["DD_A_per_100k"]]) > 1e-8) {
  stop("M3 restriction estimates do not match the formal full-definition point.")
}
restriction_labels <- c(full="Full primary",exclude_malignant_UCD="Exclude deaths with malignant UCD",
                        no_malignancy_source_any="Records with no malignant code in available fields")
f3bc <- data.frame(
  case_definition=restriction_order,
  restriction=unname(restriction_labels[restriction_order]),
  RRR=restrictions[["BR_over_US_rate_ratio"]],
  DD_per_100k=restrictions[["BR_minus_US_rate_difference_per_100k"]],
  interval_status="Descriptive point estimate; no interval computed",
  denominator="Full population", period=period, age_scope="all_age"
)
f3bc$restriction <- factor(f3bc$restriction,levels=rev(unname(restriction_labels[restriction_order])))
restriction_axis_labels <- c(
  "Full primary"="Full primary",
  "Exclude deaths with malignant UCD"="Exclude deaths with\nmalignant UCD",
  "Records with no malignant code in available fields"="No malignant code in\navailable fields"
)

p3a <- ggplot(f3a,aes(y=component,colour=component_key)) +
  geom_vline(xintercept=0,linewidth=.35,colour="black") +
  geom_segment(aes(x=bar_lower,xend=bar_upper,yend=component),linewidth=6.2,lineend="butt") +
  geom_text(aes(x=label_x,label=value_label),hjust=0,size=2.25,colour="#222222",show.legend=FALSE) +
  scale_colour_manual(values=pal[c("focus","other_positive","negative","net")],guide="none") +
  scale_x_continuous(limits=c(-.02,1.16),breaks=c(0,.25,.5,.75,1),expand=expansion(mult=c(0,.01))) +
  labs(title="Signed certificate-context partition",x="Contribution to DD_A (per 100,000)",y=NULL)
p3b <- ggplot(f3bc,aes(RRR,restriction,colour=case_definition)) +
  geom_vline(xintercept=1,linetype="dashed",linewidth=.35,colour="#7A7A7A") +
  geom_point(size=2.1) +
  geom_text(aes(label=sprintf("%.3f",RRR)),nudge_x=.012,hjust=0,size=2.05,colour="#333333",show.legend=FALSE) +
  scale_y_discrete(labels=restriction_axis_labels) +
  scale_colour_manual(values=c(full=pal[["UCD"]],exclude_malignant_UCD=pal[["restricted"]],no_malignancy_source_any="#9AA1A6"),guide="none") +
  scale_x_continuous(limits=c(.70,1.04),breaks=c(.7,.8,.9,1),expand=expansion(mult=c(.01,.02))) +
  labs(title="Relative scale",x="RRR (ratio of country rate ratios)",y=NULL)
p3c <- ggplot(f3bc,aes(DD_per_100k,restriction,colour=case_definition)) +
  geom_vline(xintercept=0,linetype="dashed",linewidth=.35,colour="#7A7A7A") +
  geom_point(size=2.1) +
  geom_text(aes(label=sprintf("%.3f",DD_per_100k)),nudge_x=.022,hjust=0,size=2.05,colour="#333333",show.legend=FALSE) +
  scale_y_discrete(labels=restriction_axis_labels) +
  scale_colour_manual(values=c(full=pal[["UCD"]],exclude_malignant_UCD=pal[["restricted"]],no_malignancy_source_any="#9AA1A6"),guide="none") +
  scale_x_continuous(limits=c(0,1.02),breaks=c(0,.25,.5,.75,1),expand=expansion(mult=c(0,.02))) +
  labs(title="Absolute scale",x="DD_A (per 100,000)",y=NULL)
fig3 <- (p3a/(p3b|p3c)) + plot_layout(heights=c(1.05,1)) + plot_annotation(tag_levels="a")
fig3 <- fig3 & theme(legend.position="none")
write_panel(f3a[,c("component_key","value_per_100k","bar_lower","bar_upper","n_classes","zero_contribution_classes_omitted","period","age_scope")],"fig3a_M1_signed_partition.csv")
write_panel(f3bc[,c("case_definition","restriction","RRR","DD_per_100k","interval_status","denominator","period","age_scope")],"fig3b_c_record_restrictions.csv")
save_pub(fig3,"Figure3_certificate_context",105)

# Figure 4
time <- d$time_points[d$time_points[["age_scope"]] == "0_74" &
                        d$time_points[["case_definition"]] == "full" &
                        d$time_points[["code_group"]] == total_group &
                        d$time_points[["endpoint"]] %in% c("UCD","any_mention"), ]
time <- time[match(c("UCD","any_mention"),time[["endpoint"]]),]
if (nrow(time) != 2L || anyNA(time[["endpoint"]])) stop("Exact 0-74 early/late points are incomplete.")
time_defs <- c("UCD","Any mention")
periods <- c("Early (2012-2014)","Late (2021-2023)")
f4a <- data.frame(
  definition=rep(time_defs,each=2), period=factor(rep(periods,2),levels=periods),
  RR_BR_over_US=c(time[["RR_early_BR_over_US"]][1],time[["RR_late_BR_over_US"]][1],
                  time[["RR_early_BR_over_US"]][2],time[["RR_late_BR_over_US"]][2]),
  age_scope="0-74",window_rule="RR derived from equal-weighted mean annual standardized rates within each country-period"
)
f4b <- data.frame(
  definition=rep(time_defs,each=2), period=factor(rep(periods,2),levels=periods),
  RD_BR_minus_US_per_100k=c(time[["RD_early_BR_minus_US_per_100k"]][1],time[["RD_late_BR_minus_US_per_100k"]][1],
                            time[["RD_early_BR_minus_US_per_100k"]][2],time[["RD_late_BR_minus_US_per_100k"]][2]),
  age_scope="0-74",window_rule="RD derived from equal-weighted mean annual standardized rates within each country-period"
)
time_ci <- d$time_intervals[
  d$time_intervals[["age_scope"]] == "0_74" & d$time_intervals[["case_definition"]] == "full" &
    d$time_intervals[["code_group"]] == total_group & d$time_intervals[["seed_role"]] == "reported" &
    d$time_intervals[["seed"]] == 20261023 & d$time_intervals[["metric"]] %in% c("I_R","I_A"), ]
time_ci <- time_ci[match(c("I_R","I_A"),time_ci[["metric"]]),]
if (nrow(time_ci) != 2L || anyNA(time_ci[["metric"]])) stop("The reported 0-74 family interaction intervals are incomplete.")
f4i <- data.frame(interaction=time_ci[["metric"]],estimate=time_ci[["estimate"]],
                  family_ci_lower=time_ci[["family_ci_lower"]],family_ci_upper=time_ci[["family_ci_upper"]],
                  family_confidence_level=time_ci[["family_confidence_level"]],
                  family_members=time_ci[["family_members"]],seed=time_ci[["seed"]],seed_role=time_ci[["seed_role"]],
                  interval_scope=time_ci[["interval_scope"]],age_scope="0-74")
ir <- f4i[f4i[["interaction"]] == "I_R",]; ia <- f4i[f4i[["interaction"]] == "I_A",]
label_ir <- sprintf("I_R = %.3f\n99.167%% family CI [%.3f, %.3f]",ir[["estimate"]],ir[["family_ci_lower"]],ir[["family_ci_upper"]])
label_ia <- sprintf("I_A = %.3f per 100,000\n99.167%% family CI [%.3f, %.3f]",ia[["estimate"]],ia[["family_ci_lower"]],ia[["family_ci_upper"]])
def_colours <- c("UCD"="#343A40","Any mention"="#737B82")
def_linetypes <- c("UCD"="solid","Any mention"="dashed")
def_shapes <- c("UCD"=16,"Any mention"=15)
p4a <- ggplot(f4a,aes(period,RR_BR_over_US,colour=definition,group=definition)) +
  geom_hline(yintercept=1,linetype="dashed",linewidth=.35,colour="#7A7A7A") +
  geom_line(aes(linetype=definition),linewidth=.62) + geom_point(aes(shape=definition),size=2.1) +
  geom_text(aes(label=sprintf("%.2f",RR_BR_over_US)),nudge_y=.11,size=2.05,show.legend=FALSE) +
  annotate("label",x=1.5,y=4.68,label=label_ir,size=2,label.size=.22,colour="#30363A",fill="white",family="Arial") +
  scale_colour_manual(values=def_colours,name="Definition") +
  scale_linetype_manual(values=def_linetypes,name="Definition") +
  scale_shape_manual(values=def_shapes,name="Definition") +
  scale_x_discrete(labels=c("Early\n2012-2014","Late\n2021-2023")) +
  scale_y_continuous(limits=c(1,5),breaks=1:5,expand=expansion(mult=c(0,.01))) +
  labs(title="Relative scale",x=NULL,y="Brazil:US rate ratio")
p4b <- ggplot(f4b,aes(period,RD_BR_minus_US_per_100k,colour=definition,group=definition)) +
  geom_hline(yintercept=0,linetype="dashed",linewidth=.35,colour="#7A7A7A") +
  geom_line(aes(linetype=definition),linewidth=.62) + geom_point(aes(shape=definition),size=2.1) +
  geom_text(aes(label=sprintf("%.2f",RD_BR_minus_US_per_100k)),nudge_y=.065,size=2.05,show.legend=FALSE) +
  annotate("label",x=1.5,y=2.15,label=label_ia,size=1.95,label.size=.22,colour="#30363A",fill="white",family="Arial") +
  scale_colour_manual(values=def_colours,name="Definition") +
  scale_linetype_manual(values=def_linetypes,name="Definition") +
  scale_shape_manual(values=def_shapes,name="Definition") +
  scale_x_discrete(labels=c("Early\n2012-2014","Late\n2021-2023")) +
  scale_y_continuous(limits=c(0,2.45),breaks=c(0,.5,1,1.5,2),expand=expansion(mult=c(0,.01))) +
  labs(title="Absolute scale",x=NULL,y="Brazil-US difference\nper 100,000")
fig4 <- (p4a|p4b) + plot_layout(widths=c(1,1)) + plot_annotation(tag_levels="a")
fig4 <- fig4 & theme(legend.position="bottom")
write_panel(f4a,"fig4a_0_74_early_late_RR.csv")
write_panel(f4b,"fig4b_0_74_early_late_RD.csv")
write_panel(f4i,"fig4_interaction_family_intervals.csv")
save_pub(fig4,"Figure4_0_74_time",88)

panel_outputs <- c(
  "Figure 1a"="fig1a_overall_rates.csv",
  "Figure 1b"="fig1b_country_rate_ratios.csv",
  "Figure 1c"="fig1c_country_rate_differences.csv",
  "Figure 2a"="fig2a_fixed_code_RRR.csv",
  "Figure 2a K83 heterogeneity"="fig2_direct_K83_heterogeneity.csv",
  "Figure 2b"="fig2b_expansion_factors.csv",
  "Figure 3a"="fig3a_M1_signed_partition.csv",
  "Figure 3b-c"="fig3b_c_record_restrictions.csv",
  "Figure 4a"="fig4a_0_74_early_late_RR.csv",
  "Figure 4b"="fig4b_0_74_early_late_RD.csv",
  "Figure 4 inference"="fig4_interaction_family_intervals.csv"
)
panel_inputs <- list(
  "Figure 1a"=c("formal_points","table1"),
  "Figure 1b"=c("formal_points","formal_intervals"),
  "Figure 1c"=c("formal_points","formal_intervals"),
  "Figure 2a"=c("formal_points","formal_intervals","table2","H"),
  "Figure 2a K83 heterogeneity"=c("H"),
  "Figure 2b"=c("formal_points","table2"),
  "Figure 3a"=c("formal_points","M1_focus","M1_240"),
  "Figure 3b-c"=c("formal_points","M3"),
  "Figure 4a"=c("time_points","time_intervals"),
  "Figure 4b"=c("time_points","time_intervals"),
  "Figure 4 inference"=c("time_intervals")
)
cat("Created Figures 1-4 and panel source data.\n")
