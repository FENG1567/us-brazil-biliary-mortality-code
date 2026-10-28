"""Integrate existing 2023 Colombia rates with the US/Brazil summaries."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


OUTPUT_NAME = "Supplement_CO_2023_integrated_bounds.csv"
REPORT_NAME = "CO_2023_INTEGRATION.json"
FIELDNAMES = [
    "year",
    "comparison",
    "age_scope",
    "age_weights",
    "sex_weights",
    "CO_UCD_rate_per_100k",
    "CO_any_rate_lower_per_100k",
    "CO_any_rate_upper_per_100k",
    "comparator_UCD_rate_per_100k",
    "comparator_any_rate_per_100k",
    "RR_UCD",
    "RR_any_lower",
    "RR_any_upper",
    "RRR_lower",
    "RRR_upper",
    "interval_type",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def one_row(rows: list[dict[str, str]], *, label: str, **criteria: str) -> dict[str, str]:
    selected = [row for row in rows if all(row.get(key) == value for key, value in criteria.items())]
    if len(selected) != 1:
        raise ValueError(f"Expected one {label} row for {criteria}; found {len(selected)}.")
    return selected[0]


def integrate(source_root: Path, output_root: Path) -> tuple[list[dict], Path, Path]:
    source_root = source_root.resolve(strict=True)
    output_root = output_root.resolve()
    co_path = source_root / "02_analysis" / "reused" / "co_2023" / "m5_microdata_standardized_rates.csv"
    usbr_path = source_root / "03_figures_tables" / "source_data" / "Supplement_annual_overall_all_age.csv"
    if not co_path.is_file() or not usbr_path.is_file():
        missing = [str(path.relative_to(source_root)) for path in (co_path, usbr_path) if not path.is_file()]
        raise FileNotFoundError("Missing required package input(s): " + ", ".join(missing))

    co_rows = read_rows(co_path)
    usbr_rows = read_rows(usbr_path)
    co_u = one_row(
        co_rows,
        label="Colombia UCD",
        year="2023",
        record_definition="full",
        code_group="K80-83_total",
        age_scope="all_age",
        sex_standardization="equal_0_5",
        endpoint="UCD",
    )
    co_any = one_row(
        co_rows,
        label="Colombia available-mention",
        year="2023",
        record_definition="full",
        code_group="K80-83_total",
        age_scope="all_age",
        sex_standardization="equal_0_5",
        endpoint="any_mention",
    )
    co_ucd_rate = float(co_u["standardized_rate_per_100k"])
    co_any_lower = float(co_any["standardized_rate_lower_per_100k"])
    co_any_upper = float(co_any["standardized_rate_upper_per_100k"])

    output_rows = []
    for country in ("US", "BR"):
        common = {
            "country": country,
            "year": "2023",
            "age_scope": "all_age",
            "case_definition": "full",
            "code_group": "K80-83_total",
        }
        country_ucd = one_row(usbr_rows, label=f"{country} UCD", **common, endpoint="UCD")
        country_any = one_row(usbr_rows, label=f"{country} available-mention", **common, endpoint="any_mention")
        comparator_ucd = float(country_ucd["standardized_rate_per_100k"])
        comparator_any = float(country_any["standardized_rate_per_100k"])
        rr_ucd = co_ucd_rate / comparator_ucd
        rr_any_lower = co_any_lower / comparator_any
        rr_any_upper = co_any_upper / comparator_any
        output_rows.append(
            {
                "year": 2023,
                "comparison": f"CO/{country}",
                "age_scope": "all_age",
                "age_weights": "WHO18",
                "sex_weights": "0.5_each",
                "CO_UCD_rate_per_100k": co_ucd_rate,
                "CO_any_rate_lower_per_100k": co_any_lower,
                "CO_any_rate_upper_per_100k": co_any_upper,
                "comparator_UCD_rate_per_100k": comparator_ucd,
                "comparator_any_rate_per_100k": comparator_any,
                "RR_UCD": rr_ucd,
                "RR_any_lower": rr_any_lower,
                "RR_any_upper": rr_any_upper,
                "RRR_lower": rr_any_lower / rr_ucd,
                "RRR_upper": rr_any_upper / rr_ucd,
                "interval_type": "source_identification_bounds_NOT_confidence_intervals",
            }
        )

    output_path = output_root / "03_figures_tables" / "source_data" / OUTPUT_NAME
    report_path = output_root / "06_review" / REPORT_NAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(output_rows)

    report = {
        "status": "PASS_ARITHMETIC_INTEGRATION",
        "scope": "Recomputed pair ratios from existing same-year, WHO18/equal-sex released country rates. No resampling or new data. Does not resolve source-axis asymmetry or nine fragments.",
        "inputs": [
            {"path": co_path.relative_to(source_root).as_posix(), "sha256": sha256_file(co_path)},
            {"path": usbr_path.relative_to(source_root).as_posix(), "sha256": sha256_file(usbr_path)},
        ],
        "result": output_rows,
        "output_sha256": sha256_file(output_path),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return output_rows, output_path, report_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    package_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--source-root",
        type=Path,
        default=package_root,
        help="Root containing 02_analysis and 03_figures_tables inputs (defaults to this package).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=package_root,
        help="Root receiving 03_figures_tables/source_data and 06_review outputs (defaults to this package).",
    )
    args = parser.parse_args()
    rows, output_path, report_path = integrate(args.source_root, args.output_root)
    print(json.dumps({"rows": len(rows), "output": output_path.name, "report": report_path.name}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
