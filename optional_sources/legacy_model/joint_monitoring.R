#!/usr/bin/env Rscript
options(stringsAsFactors=FALSE,scipen=999,digits=17,warn=1)
AGES <- c("0-4","5-9","10-14","15-19","20-24","25-29","30-34","35-39",
 "40-44","45-49","50-54","55-59","60-64","65-69","70-74","75-79","80-84","85+")
COUNTRIES <- c("US","BR"); SEXES <- c("F","M"); YEARS <- 2012:2024
REPRESENTATIONS <- c("ucd","any_common","alternative")
RATE_SCALE <- 1e5; BOOTSTRAP_DRAWS <- 2000L; BOOTSTRAP_SEED <- 20261021L
# Diagnostic agreement cutoff only; it is not an inferential gate.
BOOTSTRAP_SE_TOLERANCE <- .08; FROZEN_TOLERANCE <- 1e-8
DEFAULT_REPO <- "D:/MCOD_Research/USBR_MCOD_extension_20261021"
DEFAULT_ARCHIVE <- "D:/MCOD_Archive_20260919/2026-08-24/x20/github_repo_build"
require_analysis_packages <- function() {
  required <- c("data.table","sandwich","digest")
  missing <- required[!vapply(required,requireNamespace,logical(1),quietly=TRUE)]
  if (length(missing)) stop("Missing R packages: ",paste(missing,collapse=", "),call.=FALSE)
  expected <- c(data.table="1.14.8",sandwich="3.0.2",digest="0.6.33")
  observed <- vapply(names(expected),function(pkg) as.character(utils::packageVersion(pkg)),character(1))
  bad <- names(expected)[observed!=expected]
  if (length(bad)) stop("R package version mismatch: ",
    paste(paste0(bad,"=",observed[bad]," expected ",expected[bad]),collapse=", "),call.=FALSE)
  invisible(observed)
}
sha256_file <- function(path) {
  if (!file.exists(path)) stop("Cannot hash missing file: ",path,call.=FALSE)
  digest::digest(file=path,algo="sha256",serialize=FALSE)
}
safe_se <- function(gradient,covariance) {
  variance <- drop(crossprod(gradient,covariance%*%gradient))
  if (!is.finite(variance)||variance < -1e-8)
    stop("Invalid contrast variance: ",format(variance,digits=17),call.=FALSE)
  sqrt(max(variance,0))
}
align_design <- function(fit,newdata) {
  x <- stats::model.matrix(stats::delete.response(stats::terms(fit)),newdata,
    contrasts.arg=fit$contrasts,xlev=fit$xlevels)
  beta <- stats::coef(fit)
  if (anyNA(beta)) stop("Model contains aliased coefficients",call.=FALSE)
  aligned <- matrix(0,nrow(x),length(beta),dimnames=list(NULL,names(beta)))
  common <- intersect(colnames(x),names(beta)); aligned[,common] <- x[,common,drop=FALSE]
  missing <- setdiff(names(beta),colnames(x))
  if (length(missing)) stop("New-data design omitted: ",paste(missing,collapse=", "),call.=FALSE)
  aligned
}

target_grid <- function(weights,year_weights,include_representation=TRUE) {
  grid <- data.table::CJ(country=factor(COUNTRIES,levels=COUNTRIES),
    year=as.integer(names(year_weights)),age_group=factor(AGES,levels=AGES),
    sex=factor(SEXES,levels=SEXES),unique=TRUE)
  if (include_representation) {
    grid <- grid[,.(representation=factor(REPRESENTATIONS,levels=REPRESENTATIONS)),
      by=.(country,year,age_group,sex)]
  }
  grid <- merge(grid,weights[,.(age_group,age_weight=normalized_weight)],
    by="age_group",all.x=TRUE,sort=FALSE)
  grid[,std_weight:=age_weight*.5*unname(year_weights[as.character(year)])]
  groups <- if (include_representation) c("country","representation") else "country"
  totals <- grid[,.(sum_weight=sum(std_weight)),by=groups]
  if (anyNA(grid$std_weight)||any(abs(totals$sum_weight-1)>1e-10))
    stop("Standardization target weights do not sum to one",call.=FALSE)
  grid[,population:=1]
  grid
}

fit_stacked_model <- function(data,model_type=c("factor_year","linear_year")) {
  model_type <- match.arg(model_type)
  long <- data.table::melt(data.table::copy(data),
    id.vars=c("country","year","age_group","sex","population","cluster_id"),
    measure.vars=REPRESENTATIONS,variable.name="representation",value.name="count")
  long[,representation:=factor(representation,levels=REPRESENTATIONS)]
  if (model_type=="linear_year") long[,year_centered:=year-mean(YEARS)]
  data.table::setorder(long,cluster_id,representation)
  formula <- if (model_type=="factor_year") {
    count ~ representation*country*factor(year) +
      representation*country*age_group + representation*country*sex + offset(log(population))
  } else {
    count ~ representation*country*year_centered +
      representation*country*age_group + representation*country*sex + offset(log(population))
  }
  model_warnings <- character()
  fit <- withCallingHandlers(stats::glm(formula,data=long,family=stats::poisson(),
      x=TRUE,y=TRUE,model=TRUE),
    warning=function(w) {
      model_warnings <<- c(model_warnings,conditionMessage(w))
      invokeRestart("muffleWarning")
    })
  x <- fit$x; beta <- stats::coef(fit); rank <- qr(x)$rank
  if (!isTRUE(fit$converged)) stop(model_type," model did not converge",call.=FALSE)
  if (anyNA(beta)||rank!=ncol(x)) stop(model_type," model has aliased coefficients",call.=FALSE)
  cluster <- long$cluster_id; clusters <- length(unique(cluster))
  covariance <- sandwich::vcovCL(fit,cluster=cluster,type="HC1")
  if (any(!is.finite(covariance))) stop(model_type," HC1 covariance is non-finite",call.=FALSE)
  covariance <- (covariance+t(covariance))/2
  # Use the canonical GLM estimating functions so the score-meat reconstruction
  # matches sandwich::vcovCL exactly, including its family/link arithmetic.
  score_rows <- sandwich::estfun(fit)
  cluster_scores <- t(rowsum(score_rows,group=as.character(cluster),reorder=FALSE))
  hessian <- crossprod(x,x*as.numeric(fit$weights)); bread <- solve(hessian)
  n_obs <- nrow(x); n_coef <- ncol(x)
  # Match sandwich::vcovCL(HC1): G/(G-1) and (N-1)/(N-K).
  hc1_correction <- (clusters/(clusters-1))*((n_obs-1)/(n_obs-n_coef))
  score_covariance_hc1 <- hc1_correction*bread%*%tcrossprod(cluster_scores)%*%bread
  rel_error <- sqrt(sum((score_covariance_hc1-covariance)^2))/
    max(sqrt(sum(covariance^2)),.Machine$double.eps)
  list(fit=fit,long=long,x=x,coefficients=beta,covariance=covariance,
    cluster_scores=cluster_scores,bread=bread,cluster=as.character(cluster),
    cluster_count=clusters,hc1_correction=hc1_correction,
    score_covariance_hc1=score_covariance_hc1,score_covariance_relative_error=rel_error,
    covariance_eigen_min=min(eigen(covariance,symmetric=TRUE,only.values=TRUE)$values),
    warnings=unique(model_warnings),converged=isTRUE(fit$converged),rank=rank,
    coefficient_count=n_coef,n_obs=n_obs,formula=formula,model_type=model_type)
}

standardize_model <- function(model,weights,year_weights,include_representation=TRUE) {
  grid <- target_grid(weights,year_weights,include_representation)
  x <- align_design(model$fit,grid)
  mu <- exp(drop(x%*%model$coefficients))
  grid[,predicted_rate:=mu]
  rates <- list(); gradients <- list(); k <- 1L
  for (country in COUNTRIES) for (representation in
    if (include_representation) REPRESENTATIONS else "ucd") {
    index <- as.character(grid$country)==country
    if (include_representation) index <- index & as.character(grid$representation)==representation
    rate <- sum(grid$std_weight[index]*grid$predicted_rate[index])*RATE_SCALE
    gradient <- colSums(x[index,,drop=FALSE]*
      (grid$std_weight[index]*grid$predicted_rate[index]*RATE_SCALE))
    if (!is.finite(rate)||rate<=0) stop("Invalid standardized rate",call.=FALSE)
    key <- paste(country,representation,sep="|")
    rates[[k]] <- data.table::data.table(country=country,representation=representation,rate=rate)
    gradients[[key]] <- gradient; k <- k+1L
  }
  rate_table <- data.table::rbindlist(rates)
  log_gradients <- lapply(seq_len(nrow(rate_table)),function(i) {
    row <- rate_table[i]
    gradients[[paste(row$country,row$representation,sep="|")]]/row$rate
  })
  names(log_gradients) <- paste(rate_table$country,rate_table$representation,sep="|")
  list(table=rate_table,gradients=gradients,log_gradients=log_gradients,grid=grid,design=x)
}

annual_standardize <- function(model,weights) {
  annual <- vector("list",length(YEARS)*length(COUNTRIES)*length(REPRESENTATIONS))
  gradients <- list(); k <- 1L
  for (year in YEARS) {
    std <- standardize_model(model,weights,stats::setNames(1,as.character(year)),TRUE)
    for (i in seq_len(nrow(std$table))) {
      row <- std$table[i]; key <- paste(year,row$country,row$representation,sep="|")
      annual[[k]] <- data.table::data.table(year=year,country=row$country,
        representation=row$representation,rate=row$rate)
      gradients[[key]] <- std$gradients[[paste(row$country,row$representation,sep="|")]]
      k <- k+1L
    }
  }
  list(table=data.table::rbindlist(annual),gradients=gradients)
}

new_spec <- function(scope,measure,contrast,estimate,gradient,scale,note="",id=NULL) {
  if (is.null(id)) id <- paste(scope,measure,contrast,sep="::")
  if (!is.finite(estimate)||any(!is.finite(gradient)))
    stop("Non-finite estimate/gradient for ",id,call.=FALSE)
  list(id=id,scope=scope,measure=measure,contrast=contrast,estimate=as.numeric(estimate),
    gradient=as.numeric(gradient),scale=scale,note=note)
}

build_overall_specs <- function(std,period_note="WHO18 age standard; sex weights 0.5/0.5; years 2012-2024 equally weighted.") {
  specs <- list(); add <- function(...) specs[[length(specs)+1L]] <<- new_spec(...)
  rate <- function(country,representation) {
    hit <- as.character(std$table$country)==country &
      as.character(std$table$representation)==representation
    if (sum(hit)!=1L) stop("Missing/duplicated overall rate: ",country,"/",representation,call.=FALSE)
    std$table$rate[hit]
  }
  grad_rate <- function(country,representation)
    std$gradients[[paste(country,representation,sep="|")]]
  grad_log_rate <- function(country,representation)
    grad_rate(country,representation)/rate(country,representation)

  for (country in COUNTRIES) for (representation in REPRESENTATIONS)
    add("overall_rate","standardized_rate",paste(country,representation,sep="|"),
      rate(country,representation),grad_log_rate(country,representation),"log",
      period_note)

  rds <- list()
  for (representation in REPRESENTATIONS) {
    value <- rate("BR",representation)-rate("US",representation)
    gradient <- grad_rate("BR",representation)-grad_rate("US",representation)
    rds[[representation]] <- list(estimate=value,gradient=gradient)
    add("overall_contrast","RD",paste0("RD_BR_minus_US_",representation),
      value,gradient,"identity","Descriptive standardized rate difference per 100,000; not causal.")
  }
  dd <- rds$any_common$estimate-rds$ucd$estimate
  gdd <- rds$any_common$gradient-rds$ucd$gradient
  add("overall_contrast","DD","DD_RD_any_common_minus_ucd",dd,gdd,"identity",
    "Difference between representation-specific BR-minus-US rate differences.")
  rd_ratio <- rds$any_common$estimate/rds$ucd$estimate
  grad_log_rd_ratio <- rds$any_common$gradient/rds$any_common$estimate-
    rds$ucd$gradient/rds$ucd$estimate
  add("overall_contrast","RD_ratio","RD_any_common_over_RD_ucd",rd_ratio,
    grad_log_rd_ratio,"log","Ratio of rate differences; joint log-scale delta interval.")

  rr <- list()
  for (representation in REPRESENTATIONS) {
    value <- rate("BR",representation)/rate("US",representation)
    gradient <- grad_log_rate("BR",representation)-grad_log_rate("US",representation)
    rr[[representation]] <- list(estimate=value,gradient=gradient)
    add("overall_contrast","RR",paste0("RR_BR_over_US_",representation),
      value,gradient,"log","Ratio of jointly standardized rates.")
  }
  pairs <- list(any_common_vs_ucd=c("any_common","ucd"),
    alternative_vs_any_common=c("alternative","any_common"))
  for (label in names(pairs)) {
    pair <- pairs[[label]]
    value <- rr[[pair[1]]]$estimate/rr[[pair[2]]]$estimate
    gradient <- rr[[pair[1]]]$gradient-rr[[pair[2]]]$gradient
    add("overall_contrast","RRR",paste0("RRR_",label),value,gradient,"log",
      "Ratio of country rate ratios; stacked representations remain jointly clustered.")
  }
  specs
}

build_annual_specs <- function(annual) {
  specs <- list(); add <- function(...) specs[[length(specs)+1L]] <<- new_spec(...)
  rate <- function(year,country,representation) {
    hit <- annual$table$year==year & as.character(annual$table$country)==country &
      as.character(annual$table$representation)==representation
    if (sum(hit)!=1L) stop("Missing/duplicated annual rate: ",year,"/",country,"/",representation,call.=FALSE)
    annual$table$rate[hit]
  }
  grad_rate <- function(year,country,representation)
    annual$gradients[[paste(year,country,representation,sep="|")]]
  grad_log_rate <- function(year,country,representation)
    grad_rate(year,country,representation)/rate(year,country,representation)
  for (year in YEARS) {
    for (country in COUNTRIES) for (representation in REPRESENTATIONS)
      add("annual_rate","standardized_rate",paste(year,country,representation,sep="|"),
        rate(year,country,representation),grad_log_rate(year,country,representation),"log",
        "Single-year WHO18 age standard; sex weights 0.5/0.5.")
    for (representation in REPRESENTATIONS) {
      rd <- rate(year,"BR",representation)-rate(year,"US",representation)
      g_rd <- grad_rate(year,"BR",representation)-grad_rate(year,"US",representation)
      rr <- rate(year,"BR",representation)/rate(year,"US",representation)
      g_log_rr <- grad_log_rate(year,"BR",representation)-grad_log_rate(year,"US",representation)
      add("annual_contrast","RD",paste0(year,"|RD_BR_minus_US_",representation),
        rd,g_rd,"identity","Within-year descriptive rate difference per 100,000; not causal.")
      add("annual_contrast","RR",paste0(year,"|RR_BR_over_US_",representation),
        rr,g_log_rr,"log","Within-year ratio of jointly standardized rates.")
    }

    dd <- (rate(year,"BR","any_common")-rate(year,"US","any_common"))-
      (rate(year,"BR","ucd")-rate(year,"US","ucd"))
    g_dd <- grad_rate(year,"BR","any_common")-grad_rate(year,"US","any_common")-
      grad_rate(year,"BR","ucd")+grad_rate(year,"US","ucd")
    add("annual_contrast","DD",paste0(year,"|DD_RD_any_common_minus_ucd"),
      dd,g_dd,"identity",
      "Within-year additive difference between BR-minus-US rate differences.")

    rr_any <- rate(year,"BR","any_common")/rate(year,"US","any_common")
    rr_ucd <- rate(year,"BR","ucd")/rate(year,"US","ucd")
    g_log_rr_any <- grad_log_rate(year,"BR","any_common")-
      grad_log_rate(year,"US","any_common")
    g_log_rr_ucd <- grad_log_rate(year,"BR","ucd")-
      grad_log_rate(year,"US","ucd")
    add("annual_contrast","RRR",paste0(year,"|RRR_any_common_over_ucd"),
      rr_any/rr_ucd,g_log_rr_any-g_log_rr_ucd,"log",
      "Within-year ratio of BR/US rate ratios; jointly clustered representations.")
  }
  specs
}

build_endpoint_specs <- function(annual) {
  specs <- list(); add <- function(...) specs[[length(specs)+1L]] <<- new_spec(...)
  rate <- function(year,country,representation) {
    hit <- annual$table$year==year & as.character(annual$table$country)==country &
      as.character(annual$table$representation)==representation
    if (sum(hit)!=1L) stop("Missing/duplicated endpoint rate: ",year,"/",country,"/",representation,call.=FALSE)
    annual$table$rate[hit]
  }
  grad_rate <- function(year,country,representation)
    annual$gradients[[paste(year,country,representation,sep="|")]]
  grad_log_rate <- function(year,country,representation)
    grad_rate(year,country,representation)/rate(year,country,representation)
  for (country in COUNTRIES) for (representation in REPRESENTATIONS) {
    delta <- rate(2024,country,representation)-rate(2012,country,representation)
    g_delta <- grad_rate(2024,country,representation)-grad_rate(2012,country,representation)
    ratio <- rate(2024,country,representation)/rate(2012,country,representation)
    g_log_ratio <- grad_log_rate(2024,country,representation)-grad_log_rate(2012,country,representation)
    add("endpoint_change","rate_change",paste(country,representation,"2024_minus_2012",sep="|"),
      delta,g_delta,"identity","Two-endpoint rate difference (2024 minus 2012), not a full-window trend.")
    add("endpoint_change","rate_ratio",paste(country,representation,"2024_over_2012",sep="|"),
      ratio,g_log_ratio,"log","Two-endpoint rate ratio (2024/2012), not a full-window trend.")
  }
  for (representation in REPRESENTATIONS) {
    rd12 <- rate(2012,"BR",representation)-rate(2012,"US",representation)
    rd24 <- rate(2024,"BR",representation)-rate(2024,"US",representation)
    grd12 <- grad_rate(2012,"BR",representation)-grad_rate(2012,"US",representation)
    grd24 <- grad_rate(2024,"BR",representation)-grad_rate(2024,"US",representation)
    lrr12 <- grad_log_rate(2012,"BR",representation)-grad_log_rate(2012,"US",representation)
    lrr24 <- grad_log_rate(2024,"BR",representation)-grad_log_rate(2024,"US",representation)
    rr12 <- rate(2012,"BR",representation)/rate(2012,"US",representation)
    rr24 <- rate(2024,"BR",representation)/rate(2024,"US",representation)
    add("endpoint_change","RD_change",paste0("RD_change_2024_minus_2012_",representation),
      rd24-rd12,grd24-grd12,"identity","Change in BR-minus-US rate difference between endpoints only.")
    add("endpoint_change","RR_endpoint_ratio",paste0("RRR_2024_over_2012_",representation),
      rr24/rr12,lrr24-lrr12,"log","Ratio of BR/US rate ratios at 2024 versus 2012; endpoint only.")
  }
  dd <- function(year) {
    a <- rate(year,"BR","any_common")-rate(year,"US","any_common")
    u <- rate(year,"BR","ucd")-rate(year,"US","ucd")
    ga <- grad_rate(year,"BR","any_common")-grad_rate(year,"US","any_common")
    gu <- grad_rate(year,"BR","ucd")-grad_rate(year,"US","ucd")
    list(value=a-u,gradient=ga-gu)
  }
  d12 <- dd(2012); d24 <- dd(2024)
  add("endpoint_change","DD_change","DD_change_2024_minus_2012_any_common_vs_ucd",
    d24$value-d12$value,d24$gradient-d12$gradient,"identity",
    "Endpoint change in DD; it is not a full-window trend.")
  specs
}

fit_linear_trend <- function(data,weights) {
  model <- fit_stacked_model(data,"linear_year")
  grid <- data.table::CJ(country=factor(COUNTRIES,levels=COUNTRIES),
    age_group=factor(AGES,levels=AGES),sex=factor(SEXES,levels=SEXES),
    representation=factor(REPRESENTATIONS,levels=REPRESENTATIONS),unique=TRUE)
  grid <- merge(grid,weights[,.(age_group,age_weight=normalized_weight)],
    by="age_group",all.x=TRUE,sort=FALSE)
  grid[,std_weight:=age_weight*.5]; grid[,population:=1]
  x0_data <- data.table::copy(grid); x1_data <- data.table::copy(grid)
  x0_data[,year_centered:=0]; x1_data[,year_centered:=1]
  x0 <- align_design(model$fit,x0_data); x1 <- align_design(model$fit,x1_data)
  mu0 <- exp(drop(x0%*%model$coefficients)); mu1 <- exp(drop(x1%*%model$coefficients))
  specs <- list(); slopes <- list()
  for (country in COUNTRIES) for (representation in REPRESENTATIONS) {
    ix <- as.character(grid$country)==country & as.character(grid$representation)==representation
    r0 <- sum(grid$std_weight[ix]*mu0[ix]); r1 <- sum(grid$std_weight[ix]*mu1[ix])
    g0 <- colSums(x0[ix,,drop=FALSE]*(grid$std_weight[ix]*mu0[ix]))/r0
    g1 <- colSums(x1[ix,,drop=FALSE]*(grid$std_weight[ix]*mu1[ix]))/r1
    slope <- log(r1)-log(r0); gradient <- g1-g0
    key <- paste(country,representation,sep="|")
    slopes[[key]] <- list(estimate=slope,gradient=gradient)
    specs[[length(specs)+1L]] <- new_spec("linear_trend","log_rate_slope_per_year",
      key,slope,gradient,"identity",
      "Exploratory centered linear-time sensitivity on log rates; primary model retains factor(year).")
  }
  country_diff <- list()
  for (representation in REPRESENTATIONS) {
    br <- slopes[[paste("BR",representation,sep="|")]]
    us <- slopes[[paste("US",representation,sep="|")]]
    x <- list(estimate=br$estimate-us$estimate,gradient=br$gradient-us$gradient)
    country_diff[[representation]] <- x
    specs[[length(specs)+1L]] <- new_spec("linear_trend","BR_minus_US_log_rate_slope",
      paste0("BR_minus_US_",representation),x$estimate,x$gradient,"identity",
      "Exploratory difference in linear log-rate slopes, per year.")
  }
  pairs <- list(any_common_vs_ucd=c("any_common","ucd"),
    alternative_vs_any_common=c("alternative","any_common"))
  for (label in names(pairs)) {
    pair <- pairs[[label]]; num <- country_diff[[pair[1]]]; den <- country_diff[[pair[2]]]
    specs[[length(specs)+1L]] <- new_spec("linear_trend","log_RRR_slope_contrast_per_year",
      paste0("log_RRR_trend_",label),num$estimate-den$estimate,
      num$gradient-den$gradient,"identity",
      "Exploratory definition contrast in country log-rate slopes; not causal or policy effect.")
  }
  list(model=model,specs=specs,target=grid)
}

summarize_specs <- function(specs,covariance,beta_draws) {
  z <- stats::qnorm(.975); rows <- vector("list",length(specs))
  for (i in seq_along(specs)) {
    s <- specs[[i]]; se <- safe_se(s$gradient,covariance)
    projection <- drop(crossprod(s$gradient,beta_draws))
    if (length(projection)!=ncol(beta_draws)||any(!is.finite(projection)))
      stop("Invalid multiplier projection for ",s$id,call.=FALSE)
    if (identical(s$scale,"log")) {
      if (s$estimate<=0) stop("Log-scale estimate is not positive: ",s$id,call.=FALSE)
      center <- log(s$estimate); draws <- exp(center+projection)
      lower <- exp(center-z*se); upper <- exp(center+z*se)
    } else {
      draws <- s$estimate+projection
      lower <- s$estimate-z*se; upper <- s$estimate+z*se
    }
    q <- stats::quantile(draws,c(.025,.5,.975),names=FALSE,type=8)
    boot_se <- stats::sd(projection)
    rel <- if (se>0) abs(boot_se-se)/se else NA_real_
    status <- if (is.finite(rel)&&rel<=BOOTSTRAP_SE_TOLERANCE) "PASS" else "REVIEW"
    rows[[i]] <- data.table::data.table(id=s$id,scope=s$scope,measure=s$measure,
      contrast=s$contrast,estimate=s$estimate,scale_for_se=s$scale,standard_error=se,
      lower=lower,upper=upper,multiplier_lower=q[1],multiplier_median=q[2],
      multiplier_upper=q[3],bootstrap_se=boot_se,bootstrap_se_relative_error=rel,
      bootstrap_se_tolerance=BOOTSTRAP_SE_TOLERANCE,bootstrap_se_status=status,
      n_bootstrap=ncol(beta_draws),
      ci_method=if (s$scale=="log") "joint HC1 delta method; log scale" else "joint HC1 delta method; identity scale",
      multiplier_method="2,000-draw cluster-score Rademacher multiplier; HC1-scaled; supportive",
      note=s$note)
  }
  list(table=data.table::rbindlist(rows))
}

make_multiplier_draws <- function(model,n_draws=BOOTSTRAP_DRAWS,seed=BOOTSTRAP_SEED) {
  set.seed(seed); g <- model$cluster_count; p <- length(model$coefficients)
  multipliers <- matrix(sample(c(-1,1),g*n_draws,replace=TRUE),nrow=g,ncol=n_draws)
  multipliers <- multipliers*sqrt(model$hc1_correction)
  delta <- model$bread %*% (model$cluster_scores %*% multipliers)
  if (!all(dim(delta)==c(p,n_draws))||any(!is.finite(delta)))
    stop("Invalid cluster-score multiplier coefficient draws",call.=FALSE)
  list(delta_beta=delta,n_draws=n_draws,seed=seed,multiplier_matrix=multipliers,
    covariance_theoretical=model$score_covariance_hc1)
}

compare_frozen <- function(summary_table,frozen_dir) {
  paths <- file.path(frozen_dir,c("representation_standardized_rates_overall.csv",
    "representation_br_us_rate_ratios.csv","representation_ratio_of_rate_ratios.csv"))
  if (any(!file.exists(paths))) stop("Missing frozen comparison table in ",frozen_dir,call.=FALSE)
  normalize_rep <- function(x) {
    x[x=="system_processed_alternative"] <- "alternative"
    x
  }
  actual_rate <- summary_table[scope=="overall_rate"&measure=="standardized_rate",
    .(country=sub("\\|.*$","",contrast),representation=sub("^.*\\|","",contrast),observed=estimate)]
  expected_rate <- data.table::fread(paths[1])
  expected_rate[,representation:=normalize_rep(representation)]
  rate_check <- merge(expected_rate[,.(country,representation,expected=estimate)],actual_rate,
    by=c("country","representation"),all=TRUE,sort=TRUE)
  if (nrow(rate_check)!=6L||anyNA(rate_check))
    stop("Frozen rates did not match all 6 country-representation cells",call.=FALSE)
  rate_check[,comparison:="frozen_standardized_rate"]
  rate_check[,absolute_difference:=abs(observed-expected)]
  rate_check[,tolerance:=FROZEN_TOLERANCE]

  actual_rr <- summary_table[scope=="overall_contrast"&measure=="RR",
    .(representation=sub("^RR_BR_over_US_","",contrast),observed=estimate)]
  expected_rr <- data.table::fread(paths[2])
  expected_rr[,representation:=normalize_rep(representation)]
  rr_check <- merge(expected_rr[,.(representation,expected=br_us_rr)],actual_rr,
    by="representation",all=TRUE,sort=TRUE)
  if (nrow(rr_check)!=3L||anyNA(rr_check))
    stop("Frozen RRs did not match all 3 representations",call.=FALSE)
  rr_check[,comparison:="frozen_BR_US_RR"]; rr_check[,country:="BR/US"]
  rr_check[,absolute_difference:=abs(observed-expected)]; rr_check[,tolerance:=FROZEN_TOLERANCE]

  actual_rrr <- summary_table[scope=="overall_contrast"&measure=="RRR",
    .(contrast=sub("^RRR_","",contrast),observed=estimate)]
  expected_rrr <- data.table::fread(paths[3])
  expected_rrr[,contrast:=sub("^system_processed_alternative","alternative",contrast)]
  rrr_check <- merge(expected_rrr[,.(contrast,expected=ratio_of_rate_ratios)],actual_rrr,
    by="contrast",all=TRUE,sort=TRUE)
  if (nrow(rrr_check)!=2L||anyNA(rrr_check))
    stop("Frozen RRRs did not match both representation contrasts",call.=FALSE)
  rrr_check[,comparison:="frozen_RRR"]; rrr_check[,country:="representation contrast"]
  rrr_check[,representation:=contrast]
  rrr_check[,absolute_difference:=abs(observed-expected)]; rrr_check[,tolerance:=FROZEN_TOLERANCE]
  detail <- data.table::rbindlist(list(
    rate_check[,.(comparison,country,representation,expected,observed,absolute_difference,tolerance)],
    rr_check[,.(comparison,country,representation,expected,observed,absolute_difference,tolerance)],
    rrr_check[,.(comparison,country,representation,expected,observed,absolute_difference,tolerance)]
  ),fill=TRUE)
  detail[,status:=ifelse(absolute_difference<=tolerance,"PASS","FAIL")]
  gates <- data.table::data.table(
    check=c("frozen_six_standardized_rates","frozen_five_RR_RRR_contrasts"),
    status=c(ifelse(max(rate_check$absolute_difference)<=FROZEN_TOLERANCE,"PASS","FAIL"),
      ifelse(max(c(rr_check$absolute_difference,rrr_check$absolute_difference))<=FROZEN_TOLERANCE,"PASS","FAIL")),
    observed=c(format(max(rate_check$absolute_difference),scientific=TRUE,digits=7),
      format(max(c(rr_check$absolute_difference,rrr_check$absolute_difference)),scientific=TRUE,digits=7)),
    expected=paste0("max absolute difference <= ",format(FROZEN_TOLERANCE,scientific=TRUE)))
  list(detail=detail,gates=gates)
}

json_quote <- function(x) {
  x <- gsub("\\\\","\\\\\\\\",as.character(x))
  x <- gsub('"','\\\\"',x,fixed=TRUE)
  paste0('"',x,'"')
}
write_gates <- function(gates,json_path,csv_path,run_status,input_hash,code_hash,draw_count,seed) {
  data.table::fwrite(gates,csv_path,na="")
  scalar_json <- function(x) {
    if (length(x)==0L||is.na(x)) return("null")
    if (is.logical(x)) return(if (x) "true" else "false")
    if (is.numeric(x)) return(format(x,scientific=FALSE,trim=TRUE,digits=17))
    json_quote(x)
  }
  fields <- list(run_status=run_status,raw_data_closure="CONDITIONAL_ON_RAW_CLOSURE",
    input_manifest_sha256=input_hash,code_sha256=code_hash,
    multiplier_draws=as.numeric(draw_count),multiplier_seed=as.numeric(seed),
    gates=lapply(seq_len(nrow(gates)),function(i) list(
      check=as.character(gates$check[i]),status=as.character(gates$status[i]),
      observed=as.character(gates$observed[i]),expected=as.character(gates$expected[i]))))
  encode <- function(value) {
    if (is.list(value)) {
      if (is.null(names(value)))
        return(paste0("[",paste(vapply(value,encode,character(1)),collapse=","),"]"))
      pairs <- paste0(json_quote(names(value)),":",vapply(value,encode,character(1)))
      return(paste0("{",paste(pairs,collapse=","),"}"))
    }
    scalar_json(value)
  }
  writeLines(encode(fields),json_path,useBytes=TRUE)
}

parse_cli <- function(args) {
  config <- list(repo=DEFAULT_REPO,archive=DEFAULT_ARCHIVE,
    input=file.path(DEFAULT_ARCHIVE,"data","derived","analysis_input_936.csv"),
    weights=file.path(DEFAULT_ARCHIVE,"data","metadata","who_2000_2025_18_group_weights_correct.csv"),
    frozen=file.path(DEFAULT_ARCHIVE,"data","source_data"),
    output=file.path(DEFAULT_REPO,"outputs","statistics"),
    draws=BOOTSTRAP_DRAWS,seed=BOOTSTRAP_SEED)
  options <- c("--repo"="repo","--archive"="archive","--input"="input",
    "--weights"="weights","--frozen-source-dir"="frozen","--output-dir"="output",
    "--bootstrap-draws"="draws","--seed"="seed")
  i <- 1L
  while (i<=length(args)) {
    name <- args[[i]]
    if (!name%in%names(options)||i==length(args))
      stop("Unknown or incomplete command-line option: ",name,call.=FALSE)
    key <- unname(options[[name]]); value <- args[[i+1L]]
    if (key%in%c("draws","seed")) value <- as.integer(value)
    config[[key]] <- value; i <- i+2L
  }
  config
}

validate_inputs <- function(config) {
  expected_input <- file.path(config$archive,"data","derived","analysis_input_936.csv")
  expected_weights <- file.path(config$archive,"data","metadata","who_2000_2025_18_group_weights_correct.csv")
  if (!file.exists(expected_input)||!file.exists(expected_weights))
    stop("Expected frozen aggregate input/WHO weights are missing from the selected archive",call.=FALSE)
  if (tolower(normalizePath(config$input,winslash="/",mustWork=TRUE))!=
      tolower(normalizePath(expected_input,winslash="/",mustWork=TRUE)))
    stop("This script reads only the frozen aggregate analysis_input_936.csv; raw-record inputs are out of scope",call.=FALSE)
  if (tolower(normalizePath(config$weights,winslash="/",mustWork=TRUE))!=
      tolower(normalizePath(expected_weights,winslash="/",mustWork=TRUE)))
    stop("This script reads only the frozen WHO-18 weights file",call.=FALSE)
  expected_frozen <- file.path(config$archive,"data","source_data")
  if (tolower(normalizePath(config$frozen,winslash="/",mustWork=TRUE))!=
      tolower(normalizePath(expected_frozen,winslash="/",mustWork=TRUE)))
    stop("Frozen model comparison tables must come from the selected archive source_data directory",call.=FALSE)
  required_paths <- c(config$input,config$weights)
  if (any(!file.exists(required_paths)))
    stop("Missing aggregate input(s): ",paste(required_paths[!file.exists(required_paths)],collapse="; "),call.=FALSE)
  if (!dir.exists(config$frozen)) stop("Missing frozen comparison directory: ",config$frozen,call.=FALSE)
  data <- data.table::fread(config$input,showProgress=FALSE)
  expected_columns <- c("country","year","age_group","sex","population",REPRESENTATIONS)
  if (!all(expected_columns%in%names(data)))
    stop("Unexpected 936-cell aggregate schema; missing: ",
      paste(setdiff(expected_columns,names(data)),collapse=", "),call.=FALSE)
  if (nrow(data)!=936L) stop("Expected exactly 936 aggregate strata; found ",nrow(data),call.=FALSE)
  if (anyNA(data[,..expected_columns])) stop("Aggregate input contains missing required values",call.=FALSE)
  expected <- data.table::CJ(country=COUNTRIES,year=YEARS,age_group=AGES,sex=SEXES,unique=TRUE)
  key_cols <- c("country","year","age_group","sex")
  data[,key:=do.call(paste,c(.SD,sep="|")),.SDcols=key_cols]
  expected[,key:=do.call(paste,c(.SD,sep="|")),.SDcols=key_cols]
  if (anyDuplicated(data$key)||!setequal(data$key,expected$key))
    stop("936-cell key grid is incomplete, duplicated, or contains out-of-scope cells",call.=FALSE)
  if (any(!is.finite(data$population))||any(data$population<=0)||
      any(!is.finite(as.matrix(data[,..REPRESENTATIONS])))||
      any(as.matrix(data[,..REPRESENTATIONS])<0))
    stop("Aggregate populations/counts must be finite, populations positive, and counts nonnegative",call.=FALSE)
  if (any(data$ucd>data$any_common)||any(data$ucd>data$alternative))
    stop("UCD counts exceed at least one mention representation",call.=FALSE)
  br <- data[country=="BR"]
  if (nrow(br)!=468L||any(br$alternative!=br$any_common))
    stop("Brazil alternative must equal any_common in all 468 strata",call.=FALSE)
  data[,key:=NULL]

  weights <- data.table::fread(config$weights,showProgress=FALSE)
  if (!all(c("age_group","weight")%in%names(weights))||nrow(weights)!=length(AGES)||
      anyDuplicated(weights$age_group)||!setequal(weights$age_group,AGES)||
      any(!is.finite(weights$weight))||any(weights$weight<=0))
    stop("WHO-18 weight schema/values failed",call.=FALSE)
  if (abs(sum(weights$weight)-1)>1e-10) stop("WHO-18 weights do not sum to one",call.=FALSE)
  weights[,normalized_weight:=weight/sum(weight)]

  if (length(config$draws)!=1L||is.na(config$draws)||config$draws<100L)
    stop("At least 100 multiplier draws are required",call.=FALSE)
  if (length(config$seed)!=1L||is.na(config$seed)||config$seed<0L||
      config$seed>=.Machine$integer.max) stop("Multiplier seed must be a nonnegative integer below the R integer limit",call.=FALSE)
  list(data=data,weights=weights)
}

write_session_info <- function(path) {
  lines <- capture.output(utils::sessionInfo())
  writeLines(lines,path,useBytes=TRUE)
}

run_joint_monitoring <- function(config) {
  versions <- require_analysis_packages()
  validated <- validate_inputs(config)
  data <- validated$data
  weights <- validated$weights
  data[,`:=`(country=factor(country,levels=COUNTRIES),year=as.integer(year),
    age_group=factor(age_group,levels=AGES),sex=factor(sex,levels=SEXES),
    cluster_id=interaction(country,year,age_group,sex,drop=TRUE,lex.order=TRUE))]
  years_equal <- stats::setNames(rep(1/length(YEARS),length(YEARS)),as.character(YEARS))

  # Primary model preserves the frozen factor-year stacked Poisson specification.
  primary <- fit_stacked_model(data,"factor_year")
  overall <- standardize_model(primary,weights,years_equal,TRUE)
  annual <- annual_standardize(primary,weights)
  overall_specs <- build_overall_specs(overall)
  annual_specs <- build_annual_specs(annual)
  endpoint_specs <- build_endpoint_specs(annual)
  primary_draws <- make_multiplier_draws(primary,config$draws,config$seed)

  overall_summary <- summarize_specs(overall_specs,primary$covariance,primary_draws$delta_beta)$table
  annual_summary <- summarize_specs(annual_specs,primary$covariance,primary_draws$delta_beta)$table
  annual_summary[,interval_scope:="nominal_pointwise_not_simultaneous"]
  endpoint_summary <- summarize_specs(endpoint_specs,primary$covariance,primary_draws$delta_beta)$table
  trend <- fit_linear_trend(data,weights)
  trend_draws <- make_multiplier_draws(trend$model,config$draws,config$seed)
  trend_summary <- summarize_specs(trend$specs,trend$model$covariance,trend_draws$delta_beta)$table

  # Brazil's 2024 release vintage is unresolved in the available evidence.
  # Excluding 2024 is a robustness check only and does not classify that file.
  data_2023 <- data[year<=2023L]
  years_2023_equal <- stats::setNames(rep(1/length(2012:2023),length(2012:2023)),
    as.character(2012:2023))
  exclude_2024 <- fit_stacked_model(data_2023,"factor_year")
  exclude_2024_std <- standardize_model(exclude_2024,weights,years_2023_equal,TRUE)
  exclude_2024_specs <- build_overall_specs(exclude_2024_std,
    "WHO18 age standard; sex weights 0.5/0.5; years 2012-2023 equally weighted; 2024 omitted as a data-vintage sensitivity only.")
  exclude_2024_draws <- make_multiplier_draws(exclude_2024,config$draws,config$seed+1L)
  exclude_2024_summary <- summarize_specs(exclude_2024_specs,exclude_2024$covariance,
    exclude_2024_draws$delta_beta)$table
  exclude_2024_summary[,`:=`(sensitivity="exclude_2024",interpretation=
    "2024 excluded only as a release-vintage sensitivity; Brazil 2024 final/provisional status is not inferred.")]

  frozen <- compare_frozen(overall_summary,config$frozen)
  covariance_gate <- primary$score_covariance_relative_error <= 1e-8
  if (!covariance_gate) stop("Independent HC1 score covariance check failed: relative error = ",
    format(primary$score_covariance_relative_error,digits=8),call.=FALSE)
  if (any(frozen$gates$status!="PASS")) stop("Frozen model-point reproduction gate failed",call.=FALSE)
  if (!exclude_2024$converged||exclude_2024$rank!=exclude_2024$coefficient_count)
    stop("2012-2023 exclusion sensitivity did not converge at full rank",call.=FALSE)

  gates <- data.table::rbindlist(list(
    data.table::data.table(check="aggregate_936_grid_and_domain",status="PASS",
      observed="936 unique country-year-age-sex cells; positive population; valid counts",
      expected="exact US/BR x 2012-2024 x WHO18 x F/M grid"),
    data.table::data.table(check="brazil_alternative_equals_any_common",status="PASS",
      observed="468/468 Brazil strata",expected="exact equality"),
    frozen$gates,
    data.table::data.table(check="HC1_score_covariance_matches_sandwich_vcovCL",status="PASS",
      observed=format(primary$score_covariance_relative_error,scientific=TRUE,digits=8),
      expected="relative Frobenius error <= 1e-8"),
    data.table::data.table(check="factor_year_model_converged_and_full_rank",status="PASS",
      observed=paste0(primary$n_obs," observations; ",primary$coefficient_count," full-rank coefficients; ",
        primary$cluster_count," original-stratum clusters"),expected="converged; no aliased coefficients"),
    data.table::data.table(check="linear_year_sensitivity_converged_and_full_rank",status="PASS",
      observed=paste0(trend$model$n_obs," observations; ",trend$model$coefficient_count,
        " full-rank coefficients; ",trend$model$cluster_count," clusters"),
      expected="converged; reported as exploratory sensitivity"),
    data.table::data.table(check="exclude_2024_vintage_sensitivity_converged_and_full_rank",status="PASS",
      observed=paste0("years 2012-2023; ",exclude_2024$n_obs," stacked observations; ",
        exclude_2024$coefficient_count," coefficients; ",exclude_2024$cluster_count," clusters"),
      expected="supportive sensitivity only; no final/provisional status inferred")
  ),fill=TRUE)
  multiplier_review <- data.table::rbindlist(list(overall_summary,annual_summary,
    endpoint_summary,trend_summary,exclude_2024_summary),fill=TRUE)
  review_n <- sum(multiplier_review$bootstrap_se_status!="PASS")
  gates <- data.table::rbindlist(list(gates,
    data.table::data.table(check="multiplier_SE_approximation_review_count",status="SUPPORTIVE_REVIEW",
      observed=paste0(review_n," of ",nrow(multiplier_review)," contrasts outside the diagnostic ",
        BOOTSTRAP_SE_TOLERANCE*100,"% bootstrap-SE agreement tolerance"),
      expected="diagnostic agreement flag only; multiplier intervals are supportive and delta intervals remain primary")),fill=TRUE)

  raw_status <- "CONDITIONAL_ON_FULL_WINDOW_RAW_CLOSURE"
  input_paths <- c(analysis_input_936=config$input,who_18_weights=config$weights,
    frozen_rates=file.path(config$frozen,"representation_standardized_rates_overall.csv"),
    frozen_rr=file.path(config$frozen,"representation_br_us_rate_ratios.csv"),
    frozen_rrr=file.path(config$frozen,"representation_ratio_of_rate_ratios.csv"))
  if (any(!file.exists(input_paths))) stop("A provenance input disappeared during analysis",call.=FALSE)
  code_path <- normalizePath(sub("^--file=","",grep("^--file=",commandArgs(trailingOnly=FALSE),value=TRUE)[1]),
    winslash="/",mustWork=TRUE)
  code_hash <- sha256_file(code_path)
  input_manifest <- data.table::data.table(
    input=names(input_paths),path=normalizePath(unname(input_paths),winslash="/",mustWork=TRUE),
    bytes=as.numeric(file.info(input_paths)$size),sha256=vapply(input_paths,sha256_file,character(1)))
  manifest_hash <- digest::digest(paste(input_manifest$input,input_manifest$sha256,collapse="\n"),
    algo="sha256",serialize=FALSE)
  package_table <- data.table::data.table(package=names(versions),version=unname(versions))
  runtime_table <- data.table::data.table(
    field=c("run_status","raw_data_closure","primary_model","primary_covariance","multiplier_draws",
      "multiplier_seed","score_covariance_relative_error","frozen_max_abs_error","linear_trend_scope",
      "exclude_2024_sensitivity","2024_vintage_status","interpretation_limit","R_version",
      "script_sha256","input_manifest_sha256","model_bundle"),
    value=c("STATISTICAL_EXTENSION_CONDITIONAL_ON_FULL_WINDOW_RAW_CLOSURE",raw_status,
      paste(deparse(primary$formula),collapse=" "),"HC1 sandwich clustered by original country-year-age-sex strata",
      as.character(primary_draws$n_draws),as.character(primary_draws$seed),
      format(primary$score_covariance_relative_error,scientific=TRUE,digits=12),
      format(max(frozen$detail$absolute_difference),scientific=TRUE,digits=12),
      "Separate centered linear-time sensitivity; factor-year model remains primary",
      "2012-2023 factor-year sensitivity, using equal-year weights",
      "Brazil 2024 final/provisional status not established here; exclusion is sensitivity only",
      "Descriptive recording contrasts only; no causal effect, harmonized burden, or clinical validity claim",
      as.character(getRversion()),code_hash,manifest_hash,"joint_monitoring_model.rds"))

  repo_dir <- normalizePath(config$repo,winslash="/",mustWork=TRUE)
  expected_output <- normalizePath(file.path(repo_dir,"outputs","statistics"),
    winslash="/",mustWork=FALSE)
  out_candidate <- normalizePath(config$output,winslash="/",mustWork=FALSE)
  if (tolower(out_candidate)!=tolower(expected_output))
    stop("This task writes only to the extension outputs/statistics directory",call.=FALSE)
  if (!dir.exists(config$output)) dir.create(config$output,recursive=TRUE,showWarnings=FALSE)
  if (!dir.exists(config$output)) stop("Cannot create output directory: ",config$output,call.=FALSE)
  out_dir <- normalizePath(config$output,winslash="/",mustWork=TRUE)

  outputs <- list(
    overall=file.path(out_dir,"overall_joint_contrasts.csv"),
    annual=file.path(out_dir,"annual_joint_contrasts.csv"),
    endpoints=file.path(out_dir,"endpoint_joint_contrasts.csv"),
    trend=file.path(out_dir,"linear_trend_sensitivity.csv"),
    exclude_2024=file.path(out_dir,"exclude_2024_vintage_sensitivity.csv"),
    frozen=file.path(out_dir,"frozen_reproduction_comparison.csv"),
    gates=file.path(out_dir,"statistical_gates.csv"),
    manifest=file.path(out_dir,"input_sha256.csv"),
    runtime=file.path(out_dir,"run_metadata.csv"),
    packages=file.path(out_dir,"package_versions.csv"),
    session=file.path(out_dir,"sessionInfo.txt"),
    model=file.path(out_dir,"joint_monitoring_model.rds")
  )
  if (any(file.exists(unlist(outputs))))
    stop("Refusing to overwrite existing statistics outputs: ",
      paste(unlist(outputs)[file.exists(unlist(outputs))],collapse="; "),call.=FALSE)
  data.table::fwrite(overall_summary,outputs$overall,na="")
  data.table::fwrite(annual_summary,outputs$annual,na="")
  data.table::fwrite(endpoint_summary,outputs$endpoints,na="")
  data.table::fwrite(trend_summary,outputs$trend,na="")
  data.table::fwrite(exclude_2024_summary,outputs$exclude_2024,na="")
  data.table::fwrite(frozen$detail,outputs$frozen,na="")
  data.table::fwrite(gates,outputs$gates,na="")
  data.table::fwrite(input_manifest,outputs$manifest,na="")
  data.table::fwrite(runtime_table,outputs$runtime,na="")
  data.table::fwrite(package_table,outputs$packages,na="")
  write_session_info(outputs$session)
  model_bundle <- list(
    schema_version=1L,
    model=list(type=primary$model_type,formula=primary$formula,
      coefficients=primary$coefficients,covariance=primary$covariance,
      cluster_scores=primary$cluster_scores,cluster_ids=colnames(primary$cluster_scores),
      bread=primary$bread,hc1_correction=primary$hc1_correction,
      score_covariance_hc1=primary$score_covariance_hc1,
      score_covariance_relative_error=primary$score_covariance_relative_error,
      cluster_count=primary$cluster_count,n_obs=primary$n_obs,
      coefficient_count=primary$coefficient_count,rank=primary$rank,
      converged=primary$converged,warnings=primary$warnings,
      xlevels=primary$fit$xlevels,contrasts=primary$fit$contrasts),
    target=list(age_weights=weights[,.(age_group,weight,normalized_weight)],
      sex_weights=stats::setNames(rep(.5,length(SEXES)),SEXES),
      year_weights=years_equal,rate_scale=RATE_SCALE,
      representations=REPRESENTATIONS,standard="WHO18"),
    provenance=list(R_version=as.character(getRversion()),packages=versions,
      script_sha256=code_hash,input_manifest_sha256=manifest_hash,
      inputs=input_manifest))
  saveRDS(model_bundle,outputs$model,compress="xz")
  message("Joint monitoring inference complete; full-window raw-data closure remains conditional.")
  message("Frozen rates/RR/RRR gate PASS; HC1 score covariance relative error = ",
    format(primary$score_covariance_relative_error,scientific=TRUE,digits=4),".")
  message("Primary 2,000-draw cluster-score multiplier seed = ",primary_draws$seed,
    "; supportive SE-review items = ",review_n,"; separate 2012-2023 vintage sensitivity included.")
  message("Statistics-only outputs: ",out_dir)
  invisible(list(overall=overall_summary,annual=annual_summary,endpoints=endpoint_summary,
    trend=trend_summary,exclude_2024=exclude_2024_summary,gates=gates,outputs=outputs,
    primary_model=primary,trend_model=trend$model,exclude_2024_model=exclude_2024))
}

if (sys.nframe()==0L) {
  config <- parse_cli(commandArgs(trailingOnly=TRUE))
  run_joint_monitoring(config)
}
