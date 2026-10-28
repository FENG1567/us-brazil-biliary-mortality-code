options(stringsAsFactors = FALSE, scipen = 6)
suppressWarnings(Sys.setlocale("LC_CTYPE", "Chinese (Simplified)_China.936"))
suppressPackageStartupMessages({ library(ggplot2); library(patchwork) })

trailing_args <- commandArgs(trailingOnly = TRUE)
root_index <- match("--project-root", trailing_args)
if (!is.na(root_index)) {
  if (root_index != 1L || length(trailing_args) != 2L) {
    stop("Usage: Rscript figure_scripts/create_supplementary_figure_s1.R [--project-root PATH]")
  }
  project_root <- normalizePath(trailing_args[[root_index + 1L]], winslash = "/", mustWork = TRUE)
} else {
  script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (!length(script_arg)) stop("Could not locate this script; supply --project-root PATH.")
  script_path <- normalizePath(sub("^--file=", "", script_arg[[1L]]), winslash = "/", mustWork = TRUE)
  project_root <- normalizePath(file.path(dirname(script_path), ".."), winslash = "/", mustWork = TRUE)
}
source_path <- file.path(project_root, "03_figures_tables", "source_data", "Supplement_annual_overall_all_age.csv")
figure_dir <- file.path(project_root, "03_figures_tables", "figures")
panel_dir <- file.path(figure_dir, "source_data")
dir.create(panel_dir, recursive = TRUE, showWarnings = FALSE)

source_data <- read.csv(source_path, check.names = FALSE, stringsAsFactors = FALSE)
required <- c("country", "year", "age_scope", "case_definition", "code_group", "endpoint",
              "raw_deaths_in_scope", "standardized_rate_per_100k", "rate_unit", "interval_status")
if (any(!required %in% names(source_data))) stop("The annual all-age source table is missing required fields.")
source_data$year <- as.integer(source_data$year)
if (nrow(source_data) != 52L || anyNA(source_data$year) ||
    !identical(sort(unique(source_data$year)), 2012:2024) ||
    any(source_data$age_scope != "all_age") || any(source_data$case_definition != "full") ||
    any(source_data$code_group != "K80-83_total") ||
    !setequal(source_data$country, c("US", "BR")) ||
    !setequal(source_data$endpoint, c("UCD", "any_mention")) ||
    any(source_data$interval_status != "ANNUAL_RATE_CI_NOT_COMPUTED")) {
  stop("Annual source-table contents do not match the expected all-age UCD/available-mention series.")
}

plot_data <- source_data[source_data$year <= 2023L, ]
excluded <- source_data[source_data$year > 2023L, ]
if (nrow(plot_data) != 48L || nrow(excluded) != 4L || any(excluded$year != 2024L) ||
    any(table(plot_data$year) != 4L) || anyNA(plot_data$standardized_rate_per_100k)) {
  stop("Unexpected annual-series selection; expected 48 displayed and four 2024 observations excluded.")
}
country_names <- c(US = "United States", BR = "Brazil")
country_colours <- c(US = "#2B6F9B", BR = "#C66B2B")
plot_data$country_label <- unname(country_names[plot_data$country])
plot_data$definition <- factor(ifelse(plot_data$endpoint == "UCD", "UCD", "Any mention"),
                               levels = c("UCD", "Any mention"))
plot_data <- plot_data[order(plot_data$country, plot_data$year, plot_data$definition), ]

panel_source <- data.frame(
  country = plot_data$country,
  country_label = plot_data$country_label,
  year = plot_data$year,
  definition = as.character(plot_data$definition),
  endpoint = plot_data$endpoint,
  raw_deaths_in_scope = plot_data$raw_deaths_in_scope,
  standardized_rate_per_100k = plot_data$standardized_rate_per_100k,
  rate_unit = plot_data$rate_unit,
  interval_status = plot_data$interval_status,
  stringsAsFactors = FALSE
)
panel_name <- "supp_fig_s1_annual_all_age_panel.csv"
excluded_name <- "supp_fig_s1_excluded_2024_observations.csv"
panel_path <- file.path(panel_dir, panel_name)
excluded_path <- file.path(panel_dir, excluded_name)
write.csv(panel_source, panel_path, row.names = FALSE, na = "", fileEncoding = "UTF-8")
excluded$exclusion_reason <- "Year 2024 is outside the 2012-2023 primary version window; the original annual source table remains unchanged."
write.csv(excluded, excluded_path, row.names = FALSE, na = "", fileEncoding = "UTF-8")

theme_pub <- theme_classic(base_size = 7, base_family = "Arial") +
  theme(axis.line = element_line(linewidth = 0.32, colour = "black"),
        axis.ticks = element_line(linewidth = 0.32, colour = "black"),
        axis.title = element_text(size = 6.6, colour = "black"),
        axis.text = element_text(size = 6, colour = "black"),
        plot.title = element_text(size = 7.2, face = "bold", colour = "black", hjust = 0,
                                  margin = margin(b = 2, unit = "mm")),
        plot.subtitle = element_text(size = 6, colour = "#333333"),
        plot.tag = element_text(size = 8, face = "bold", colour = "black"),
        plot.margin = margin(2.5, 9, 2, 2.5, unit = "mm"),
        panel.grid = element_blank())
theme_set(theme_pub)

make_country_panel <- function(country_code) {
  dat <- plot_data[plot_data$country == country_code, ]
  endpoint_labels <- dat[dat$year == 2023L, ]
  ggplot(dat, aes(x = year, y = standardized_rate_per_100k, group = definition)) +
    geom_line(aes(linetype = definition), colour = unname(country_colours[[country_code]]),
              linewidth = 0.62, lineend = "round") +
    geom_point(aes(shape = country), colour = unname(country_colours[[country_code]]),
               size = 1.55, stroke = 0.25) +
    geom_text(data = endpoint_labels, aes(label = definition),
              colour = unname(country_colours[[country_code]]), size = 2.15,
              hjust = 0, nudge_x = 0.18, show.legend = FALSE) +
    scale_linetype_manual(values = c("UCD" = "solid", "Any mention" = "dashed"), guide = "none") +
    scale_shape_manual(values = c(US = 16, BR = 17), guide = "none") +
    scale_x_continuous(breaks = c(seq(2012, 2022, by = 2), 2023),
                       expand = expansion(mult = c(0, 0))) +
    scale_y_continuous(expand = expansion(mult = c(0.05, 0.13))) +
    coord_cartesian(xlim = c(2012, 2024.15), clip = "off") +
    labs(title = unname(country_names[[country_code]]), x = "Year",
         y = "Standardized rate\n(per 100,000)")
}

fig_s1 <- (make_country_panel("US") | make_country_panel("BR")) +
  plot_layout(widths = c(1, 1)) +
  plot_annotation(title = "Annual all-age standardized biliary mortality rates",
                  subtitle = "2012-2023; independent y scales by country; descriptive estimates",
                  tag_levels = "a") & theme_pub

save_pub_r <- function(plot, filename, width_mm = 183, height_mm = 90, dpi = 600) {
  w <- width_mm / 25.4
  h <- height_mm / 25.4
  svglite::svglite(paste0(filename, ".svg"), width = w, height = h, bg = "white")
  print(plot); grDevices::dev.off()
  grDevices::cairo_pdf(paste0(filename, ".pdf"), width = w, height = h,
                       family = "Arial", bg = "white")
  print(plot); grDevices::dev.off()
  ragg::agg_tiff(paste0(filename, ".tiff"), width = w, height = h, units = "in",
                 res = dpi, background = "white", compression = "lzw")
  print(plot); grDevices::dev.off()
  ragg::agg_png(paste0(filename, ".png"), width = w, height = h, units = "in",
                res = dpi, background = "white")
  print(plot); grDevices::dev.off()
}
stem <- "SupplementaryFigureS1_annual_all_age"
save_pub_r(fig_s1, file.path(figure_dir, stem), width_mm = 183, height_mm = 90, dpi = 600)


cat("Created Supplementary Figure S1; source rows:", nrow(source_data),
    "displayed:", nrow(plot_data), "excluded:", nrow(excluded),
    "(2024 outside the primary version window).\n")
