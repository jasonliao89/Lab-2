"""SEC EDGAR data-collection CLI for an equity-research workflow."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os

SEC_ROOT = "https://www.sec.gov"
DATA_ROOT = "https://data.sec.gov"
FORMS = ("10-K", "10-Q", "8-K")

# These are XBRL taxonomy concepts, not issuer-specific labels.
CONCEPTS: dict[str, list[str]] = {
    "Total revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "Operating income": ["OperatingIncomeLoss"],
    "Net income": ["NetIncomeLoss", "ProfitLoss"],
    "Diluted EPS": ["EarningsPerShareDiluted"],
    "Stockholders' equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "Long-term debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "Current portion of long-term debt": ["LongTermDebtCurrent", "LongTermDebtAndFinanceLeaseObligationsCurrent"],
    "Cash from operations": ["NetCashProvidedByUsedInOperatingActivities"],
    "Additions to property and equipment": ["PaymentsToAcquirePropertyPlantAndEquipment"],
}


class EdgarClient:
    def __init__(self, user_agent: str) -> None:
        if not user_agent.strip():
            raise ValueError("SEC_USER_AGENT is empty. Set it in .env (include a contact email).")
        self.session = requests.Session()
        # Applied at the session level so every SEC request carries the identity.
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

    def get_json(self, url: str) -> dict[str, Any]:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.text


def cik10(value: str | int) -> str:
    return str(value).zfill(10)


def resolve_ticker(client: EdgarClient, ticker: str) -> dict[str, str]:
    payload = client.get_json(f"{SEC_ROOT}/files/company_tickers.json")
    needle = ticker.upper()
    for item in payload.values():
        if item["ticker"].upper() == needle:
            return {"ticker": needle, "cik": cik10(item["cik_str"]), "name": item["title"]}
    raise LookupError(f"Ticker {ticker!r} was not found in SEC company_tickers.json.")


def rows_from_submissions(payload: dict[str, Any]) -> list[dict[str, str]]:
    recent = payload.get("filings", {}).get("recent", {})
    keys = ("accessionNumber", "form", "primaryDocument", "reportDate", "filingDate", "items")
    return [{key: str(recent.get(key, [""] * len(recent.get("form", [])))[i] or "") for key in keys}
            for i in range(len(recent.get("form", [])))]


def submissions(client: EdgarClient, cik: str) -> list[dict[str, str]]:
    payload = client.get_json(f"{DATA_ROOT}/submissions/CIK{cik}.json")
    rows = rows_from_submissions(payload)
    # Older filing shards are fetched too, so "latest" remains correct for sparse issuers.
    for shard in payload.get("filings", {}).get("files", []):
        rows.extend(rows_from_submissions(client.get_json(f"{DATA_ROOT}/submissions/{shard['name']}")))
    return rows


def latest_filings(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    selected: dict[str, dict[str, str]] = {}
    for form in ("10-K", "10-Q"):
        choices = [row for row in rows if row["form"] == form]
        if choices:
            selected[form] = max(choices, key=lambda row: (row["filingDate"], row["accessionNumber"]))
    releases = [row for row in rows if row["form"] == "8-K" and "2.02" in row["items"]]
    if releases:
        selected["8-K"] = max(releases, key=lambda row: (row["filingDate"], row["accessionNumber"]))
    missing = set(FORMS) - set(selected)
    if missing:
        raise LookupError("Could not locate: " + ", ".join(sorted(missing)))
    return selected


def archive_base(cik: str, accession: str) -> str:
    return f"{SEC_ROOT}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"


def acceptance_time(client: EdgarClient, cik: str, filing: dict[str, str]) -> str:
    index_url = archive_base(cik, filing["accessionNumber"]) + "/" + filing["accessionNumber"] + "-index.html"
    page = BeautifulSoup(client.get_text(index_url), "html.parser").get_text(" ", strip=True)
    match = re.search(r"Accepted\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9:]+)", page, re.I)
    return match.group(1) if match else "not found on index page"


def index_files(client: EdgarClient, cik: str, filing: dict[str, str]) -> list[str]:
    url = archive_base(cik, filing["accessionNumber"]) + "/index.json"
    return [item["name"] for item in client.get_json(url).get("directory", {}).get("item", [])]


def document_name(client: EdgarClient, cik: str, filing: dict[str, str]) -> str:
    if filing["form"] != "8-K":
        return filing["primaryDocument"]
    files = index_files(client, cik, filing)
    exhibit = next((f for f in files if re.search(r"ex[-_]?99[._-]?1", f, re.I) and f.lower().endswith((".htm", ".html", ".txt"))), None)
    if not exhibit:
        raise LookupError("The Item 2.02 8-K does not have an Exhibit 99.1 HTML/text file.")
    return exhibit


def html_to_text(source: str) -> str:
    soup = BeautifulSoup(source, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def download_texts(client: EdgarClient, cik: str, filings: dict[str, dict[str, str]], destination: Path) -> dict[str, str]:
    destination.mkdir(parents=True, exist_ok=True)
    texts: dict[str, str] = {}
    for form, filing in filings.items():
        filename = document_name(client, cik, filing)
        source = client.get_text(archive_base(cik, filing["accessionNumber"]) + "/" + filename)
        plain = html_to_text(source)
        label = "8-K_Exhibit_99.1" if form == "8-K" else form
        (destination / f"{label}.txt").write_text(plain, encoding="utf-8")
        texts[form] = plain
    return texts


def annual_records(fact: dict[str, Any]) -> list[dict[str, Any]]:
    records = [r for unit in fact.get("units", {}).values() for r in unit
               if r.get("form") == "10-K" and r.get("fp") == "FY" and r.get("fy")]
    # ``fy`` identifies the filing's fiscal year, including comparative facts.
    # The statement period end is the reliable key for the reported fiscal year.
    # Amendments can duplicate a period; retain the latest filed instance.
    by_end: dict[str, dict[str, Any]] = {}
    for record in records:
        end = record.get("end", "")
        if end and (end not in by_end or record.get("filed", "") > by_end[end].get("filed", "")):
            by_end[end] = record
    return list(by_end.values())


def choose_concept(facts: dict[str, Any], tags: list[str], target_end: str | None = None) -> tuple[str | None, list[dict[str, Any]]]:
    candidates = []
    for tag in tags:
        fact = facts.get("facts", {}).get("us-gaap", {}).get(tag)
        if fact:
            records = annual_records(fact)
            if records:
                candidates.append((tag, records))
    if not candidates:
        return None, []
    if target_end is not None:
        same_period = [(tag, rows) for tag, rows in candidates if any(r.get("end") == target_end for r in rows)]
        if same_period:
            candidates = same_period
    return max(candidates, key=lambda pair: max(r["end"] for r in pair[1]))


def company_facts(client: EdgarClient, cik: str) -> dict[str, Any]:
    return client.get_json(f"{DATA_ROOT}/api/xbrl/companyfacts/CIK{cik}.json")


def collect_facts(payload: dict[str, Any]) -> tuple[int, str, str | None, dict[str, dict[str, Any] | None]]:
    revenue_tag, revenue_rows = choose_concept(payload, CONCEPTS["Total revenue"])
    if not revenue_tag:
        raise LookupError("No annual revenue concept was found in company facts.")
    revenue_by_end = sorted(revenue_rows, key=lambda row: row["end"])
    current_end = revenue_by_end[-1]["end"]
    prior_end = revenue_by_end[-2]["end"] if len(revenue_by_end) > 1 else None
    year = int(current_end[:4])
    output: dict[str, dict[str, Any] | None] = {}
    for label, tags in CONCEPTS.items():
        # Select the candidate tag with the newest actual statement period.
        # Values are then read only at revenue's current and prior period ends.
        tag, records = choose_concept(payload, tags)
        values: dict[str, Any] = {"tag": tag, "current": None, "prior": None}
        if tag:
            for record in records:
                if record.get("end") in (current_end, prior_end):
                    key = "current" if record["end"] == current_end else "prior"
                    values[key] = {k: record.get(k) for k in ("val", "end", "filed", "form", "accn", "fy", "fp")}
        output[label] = values
    return year, current_end, prior_end, output


def number(value: Any) -> float | None:
    return float(value) if value is not None else None


def fact_value(facts: dict[str, dict[str, Any] | None], label: str, period: str = "current") -> float | None:
    item = facts[label]
    return number(item[period]["val"]) if item and item.get(period) else None


def ratio(label: str, numerator: float | None, denominator: float | None, percent: bool = True) -> str:
    if numerator is None or denominator in (None, 0):
        return f"{label}: unavailable"
    result = numerator / denominator
    shown = f"{result:.2%}" if percent else f"{result:.4f}"
    return f"{label}: {numerator:,.2f} / {denominator:,.2f} = {shown}"


def calculator(facts: dict[str, dict[str, Any] | None], price: float | None) -> list[str]:
    revenue, revenue_prior = fact_value(facts, "Total revenue"), fact_value(facts, "Total revenue", "prior")
    operating, income = fact_value(facts, "Operating income"), fact_value(facts, "Net income")
    equity, equity_prior = fact_value(facts, "Stockholders' equity"), fact_value(facts, "Stockholders' equity", "prior")
    debt, current_debt = fact_value(facts, "Long-term debt"), fact_value(facts, "Current portion of long-term debt")
    cfo, capex, eps = fact_value(facts, "Cash from operations"), fact_value(facts, "Additions to property and equipment"), fact_value(facts, "Diluted EPS")
    growth = None if revenue is None or revenue_prior in (None, 0) else (revenue - revenue_prior) / revenue_prior
    avg_equity = None if equity is None or equity_prior is None else (equity + equity_prior) / 2
    total_debt = None if debt is None or current_debt is None else debt + current_debt
    lines = [
        f"Revenue growth: ({revenue:,.2f} - {revenue_prior:,.2f}) / {revenue_prior:,.2f} = {growth:.2%}" if growth is not None else "Revenue growth: unavailable",
        ratio("Operating margin", operating, revenue),
        ratio("Return on average equity", income, avg_equity),
        ratio("Debt to equity (including current portion)", total_debt, equity),
        f"Free cash flow: {cfo:,.2f} - {capex:,.2f} = {cfo - capex:,.2f}" if cfo is not None and capex is not None else "Free cash flow: unavailable",
        f"P/E: {price:,.2f} / {eps:,.2f} = {price / eps:.2f}" if price is not None and eps not in (None, 0) else "P/E: unavailable (provide --price and a nonzero diluted EPS)",
    ]
    return lines


def release_lines(text: str) -> list[str]:
    lines = text.splitlines()
    searches = {
        "Quarter revenue": (
            r"\b(?:total\s+revenue|total\s+revenues|net\s+sales|revenue)\b.*\b(?:quarter|three\s+months)\b",
            r"\b(?:quarter|three\s+months)\b.*\b(?:total\s+revenue|total\s+revenues|net\s+sales|revenue)\b",
        ),
        "Quarter diluted EPS": (
            r"\b(?:quarter|three\s+months)\b.*\b(?:diluted\s+(?:earnings\s+per\s+share|eps)|per\s+diluted\s+share)\b",
            r"\b(?:diluted\s+(?:earnings\s+per\s+share|eps)|per\s+diluted\s+share)\b.*\b(?:quarter|three\s+months)\b",
        ),
    }
    found = []
    for label, patterns in searches.items():
        match = next((line for pattern in patterns for line in lines if re.search(pattern, line, re.I)), None)
        found.append(f"{label}: {match}" if match else f"{label}: not found in release text.")
    return found


def print_facts(year: int, current_end: str, prior_end: str | None, facts: dict[str, dict[str, Any] | None]) -> None:
    print(f"Fiscal year {year}, ending {current_end} (prior period end: {prior_end or 'unavailable'})")
    for label, data in facts.items():
        if not data or not data["tag"]:
            print(f"  {label}: unavailable")
            continue
        for which in ("current", "prior"):
            value = data.get(which)
            if value:
                print(f"  {label} [{which}] - {data['tag']}: {value['val']} | end {value['end']} | filed {value['filed']} | {value['form']} {value['accn']}")
            else:
                print(f"  {label} [{which}] - {data['tag']}: unavailable")


def output_directory(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "issuer"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("research_output") / f"{safe}_{stamp}"


def save_facts(destination: Path, issuer: dict[str, str], year: int, current_end: str, prior_end: str | None,
               facts: dict[str, dict[str, Any] | None],
               peers: dict[str, dict[str, Any]]) -> Path:
    """Keep primary and peer XBRL selections together with the filing texts."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "facts.json"
    target.write_text(json.dumps({
        "issuer": {"ticker": issuer["ticker"], "cik": issuer["cik"], "fiscal_year": year,
                   "period_end": current_end, "prior_period_end": prior_end, "facts": facts},
        "peers": peers,
    }, indent=2), encoding="utf-8")
    return target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect SEC filing text and annual XBRL facts for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("--peers", nargs="+", default=[],
                        help="Peer tickers, separated by spaces and/or commas (for example WMT,TGT,BJ).")
    parser.add_argument("--price", type=float)
    parser.add_argument("--step", type=int, choices=range(1, 8), help="Run only this numbered step (dependencies are fetched silently).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # Accept both conventional shell-separated lists and the compact comma form.
    args.peers = [ticker.strip().upper() for value in args.peers for ticker in value.split(",") if ticker.strip()]
    load_dotenv()
    client = EdgarClient(os.getenv("SEC_USER_AGENT", ""))
    one_step = args.step
    issuer = resolve_ticker(client, args.ticker)
    if one_step in (None, 1):
        print(f"STEP 1 - {issuer['ticker']} -> CIK {issuer['cik']}")

    need_filings = one_step in (None, 2, 3, 6)
    filings = latest_filings(submissions(client, issuer["cik"])) if need_filings else None
    if one_step in (None, 2):
        print("STEP 2 - selected filings")
        for form, filing in filings.items():
            print(f"  {form}: {filing['accessionNumber']} | Accepted {acceptance_time(client, issuer['cik'], filing)}")

    destination = output_directory(issuer["name"])
    texts: dict[str, str] | None = None
    peer_results: dict[str, dict[str, Any]] = {}
    if one_step in (None, 3):
        texts = download_texts(client, issuer["cik"], filings, destination)
        print(f"STEP 3 - saved plain-text filings in {destination.resolve()}")

    need_facts = one_step in (None, 4, 5, 7)
    year: int | None = None
    current_end: str | None = None
    prior_end: str | None = None
    facts: dict[str, dict[str, Any] | None] | None = None
    if need_facts:
        year, current_end, prior_end, facts = collect_facts(company_facts(client, issuer["cik"]))
    if one_step in (None, 4):
        facts_path = save_facts(destination, issuer, year, current_end, prior_end, facts, peer_results)
        print("STEP 4 - annual company facts")
        print_facts(year, current_end, prior_end, facts)
        print(f"  Saved {facts_path.resolve()}")
    if one_step in (None, 5):
        print("STEP 5 - calculations")
        for line in calculator(facts, args.price): print("  " + line)
    if one_step in (None, 6):
        if texts is None:
            # Step-only mode reads the release without writing unrelated artifacts.
            name = document_name(client, issuer["cik"], filings["8-K"])
            texts = {"8-K": html_to_text(client.get_text(archive_base(issuer["cik"], filings["8-K"]["accessionNumber"]) + "/" + name))}
        print("STEP 6 - earnings-release lines")
        for line in release_lines(texts["8-K"]): print("  " + line)
    if one_step in (None, 7):
        print("STEP 7 - peers (same annual XBRL definition)")
        for ticker in args.peers:
            peer = resolve_ticker(client, ticker)
            peer_year, peer_end, peer_prior_end, peer_facts = collect_facts(company_facts(client, peer["cik"]))
            revenue, operating = fact_value(peer_facts, "Total revenue"), fact_value(peer_facts, "Operating income")
            peer_results[peer["ticker"]] = {"cik": peer["cik"], "fiscal_year": peer_year,
                                             "period_end": peer_end, "prior_period_end": peer_prior_end,
                                             "facts": peer_facts}
            print(f"  {peer['ticker']} | CIK {peer['cik']} | FY {peer_year}")
            print_facts(peer_year, peer_end, peer_prior_end, peer_facts)
            print("  " + ratio("Operating margin", operating, revenue))
        facts_path = save_facts(destination, issuer, year, current_end, prior_end, facts, peer_results)
        print(f"  Saved {facts_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, LookupError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
