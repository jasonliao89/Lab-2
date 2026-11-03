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
from openai import OpenAI, OpenAIError

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
    """Return the Exhibit 99.1 filename for an Item 2.02 8-K."""
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
        base = archive_base(cik, filing["accessionNumber"])
        if form == "8-K":
            # Preserve the cover filing as well as the release users actually
            # analyze; an 8-K's primary document and Exhibit 99.1 are distinct.
            primary = html_to_text(client.get_text(base + "/" + filing["primaryDocument"]))
            (destination / "8-K.txt").write_text(primary, encoding="utf-8")
            filename = document_name(client, cik, filing)
            release = html_to_text(client.get_text(base + "/" + filename))
            (destination / "8-K_Exhibit_99.1.txt").write_text(release, encoding="utf-8")
            texts[form] = release
        else:
            plain = html_to_text(client.get_text(base + "/" + filing["primaryDocument"]))
            (destination / f"{form}.txt").write_text(plain, encoding="utf-8")
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
            r"\b(?:total\s+)?(?:revenue|revenues|net\s+sales)\s+(?:was|were|totaled|amounted\s+to|of)\b",
            r"\b(?:total\s+revenue|total\s+revenues|net\s+sales|revenue)\b.*\b(?:quarter|three\s+months)\b",
            r"\b(?:quarter|three\s+months)\b.*\b(?:total\s+revenue|total\s+revenues|net\s+sales|revenue)\b",
        ),
        "Quarter diluted EPS": (
            r"\b(?:diluted\s+(?:earnings\s+per\s+share|eps)|per\s+diluted\s+share)\s+(?:was|were|of)\b",
            r"\b(?:quarter|three\s+months)\b.*\b(?:diluted\s+(?:earnings\s+per\s+share|eps)|per\s+diluted\s+share)\b",
            r"\b(?:diluted\s+(?:earnings\s+per\s+share|eps)|per\s+diluted\s+share)\b.*\b(?:quarter|three\s+months)\b",
        ),
    }
    found: list[str] = []
    for label, patterns in searches.items():
        match = next((line for pattern in patterns for line in lines if re.search(pattern, line, re.I)), None)
        if not match:
            raise LookupError(f"Step 6 cannot complete: {label} was not found in the earnings release.")
        found.append(match)
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


def numbered(text: str, source: str) -> str:
    return "\n".join(f"[{source} L{i:05d}] {line}" for i, line in enumerate(text.splitlines(), 1))


def section(text: str, start: str, end: str) -> str:
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if re.search(start, line, re.I)), None)
    if first is None:
        return text
    last = next((i for i in range(first + 1, len(lines)) if re.search(end, lines[i], re.I)), len(lines))
    return "\n".join(lines[first:last])


def json_output(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Model did not return valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("Model JSON output must be an object.")
    return value


def call_role(ai: OpenAI, model: str, name: str, instructions: str, packet: dict[str, Any], markdown: bool = False) -> Any:
    """Every specialist sees only a Strategy Manager JSON packet."""
    print(name)
    response = ai.responses.create(model=model, instructions=instructions, input=json.dumps(packet, ensure_ascii=False))
    return response.output_text if markdown else json_output(response.output_text)


def requested_calculations(requests_: Any, facts: dict[str, dict[str, Any] | None]) -> list[str]:
    """Execute only fact-label/period arithmetic requested by the Financial Analyst."""
    results: list[str] = []
    for request in requests_ if isinstance(requests_, list) else []:
        if not isinstance(request, dict):
            continue
        operation = request.get("operation", "ratio")
        left, right = request.get("numerator", {}), request.get("denominator", {})
        if not isinstance(left, dict) or not isinstance(right, dict):
            results.append("Calculator request rejected: use numerator/denominator fact and period objects.")
            continue
        try:
            numerator = fact_value(facts, left["fact"], left.get("period", "current"))
            denominator = fact_value(facts, right["fact"], right.get("period", "current"))
        except KeyError:
            results.append("Calculator request rejected: unknown fact label.")
            continue
        name = request.get("name", "Requested calculation")
        if operation == "difference" and numerator is not None and denominator is not None:
            results.append(f"{name}: {numerator:,.2f} - {denominator:,.2f} = {numerator - denominator:,.2f}")
        else:
            results.append(ratio(name, numerator, denominator))
    return results


MANAGER = """You are the Strategy Manager. Use only the supplied JSON. For dispatches, produce JSON packets with exactly task, context, expected_output. Preserve source citations. Never add outside facts."""
RESEARCH = """You are the Research Analyst. Use only the supplied manager JSON packet; do not use background knowledge. Return JSON with overview, market_positioning_growth, future_developments, evidence_gaps. Cover business model, competitive position, catalysts, and recent developments. Every factual claim must include a verbatim quote and its supplied [source Lxxxxx] citation."""
FINANCIAL = """You are the Financial Analyst. Use only the supplied manager JSON packet. Return JSON with valuation_insights, financial_analysis, peer_comparison, calculator_requests, evidence_gaps. Every number must state its filing plus supplied tag or line and period; every ratio must reproduce supplied arithmetic. State competing definitions when given. Never calculate in the model. For more ratios, calculator_requests must be objects with name, operation (ratio or difference), and numerator/denominator objects containing fact and period; wait for calculator results."""
ADVISOR = """You are the Investment Advisor. Use only the supplied manager JSON packet. Return JSON with thesis, recommendation (Buy, Hold, or Sell), deciding_number, evidence. Use exactly one deciding number, cited as supplied. Do not introduce facts or calculate."""
QA = """You are the Strategy Manager conducting QA. Use only supplied JSON. Return {\"repairs\": []} or repairs with at most one packet per role. Each repair has role (Research Analyst, Financial Analyst, Investment Advisor) and packet (task, context, expected_output). Repair only missing or inconsistent parts among Overview; Market Positioning and Growth Outlook; Valuation Insights; Future Developments and Strategic Recommendations; Investment Recommendation."""
REPORT = """You are the Strategy Manager. Use only supplied JSON and return Markdown only. Write exactly five H2 sections: Overview; Market Positioning and Growth Outlook; Valuation Insights; Future Developments and Strategic Recommendations; Investment Recommendation. Retain citations and reconcile inconsistencies; add no outside facts."""


def run_roles(destination: Path, issuer: dict[str, str], filings: dict[str, dict[str, str]], texts: dict[str, str],
              facts: dict[str, Any], ratios: list[str], release: list[str], peers: dict[str, Any]) -> Path:
    api_key = os.getenv("API_KEY", "")
    if not api_key.strip():
        raise ValueError("API_KEY is empty. Set it in .env to run the model roles.")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1")
    ai = OpenAI(api_key=api_key)
    ten_k = texts["10-K"]
    evidence = {
        "filings": {form: item["accessionNumber"] for form, item in filings.items()},
        "research_sources": {
            "10-K Business": numbered(section(ten_k, r"^item\s+1[.\s:-]+business", r"^item\s+1a|^item\s+2"), "10-K Business"),
            "10-K MD&A": numbered(section(ten_k, r"^item\s+7[.\s:-]+.*(?:management|discussion)", r"^item\s+7a|^item\s+8"), "10-K MD&A"),
            "8-K Exhibit 99.1": numbered(texts["8-K"], "8-K Exhibit 99.1"),
        },
        "financial_sources": {"facts": facts, "ratios": ratios, "quarter_release_lines": release, "peers": peers},
    }
    dispatch = call_role(ai, model, "Strategy Manager - dispatch", MANAGER,
                         {"task": "Create research_packet and financial_packet from SEC evidence.", "context": evidence,
                          "expected_output": "JSON with research_packet and financial_packet; each is task/context/expected_output."})
    if not isinstance(dispatch.get("research_packet"), dict) or not isinstance(dispatch.get("financial_packet"), dict):
        raise ValueError("Strategy Manager did not provide both specialist packets.")
    research = call_role(ai, model, "Research Analyst", RESEARCH, dispatch["research_packet"])
    financial = call_role(ai, model, "Financial Analyst", FINANCIAL, dispatch["financial_packet"])
    calculations = requested_calculations(financial.get("calculator_requests"), facts)
    if calculations:
        calc_handoff = call_role(ai, model, "Strategy Manager - calculator handoff", MANAGER,
                                 {"task": "Send the Financial Analyst only the approved calculator results and its prior analysis for revision.",
                                  "context": {"financial_output": financial, "calculator_results": calculations},
                                  "expected_output": {"financial_packet": {"task": "", "context": {}, "expected_output": ""}}})
        if isinstance(calc_handoff.get("financial_packet"), dict):
            financial = call_role(ai, model, "Financial Analyst - calculator revision", FINANCIAL, calc_handoff["financial_packet"])
    handoff = call_role(ai, model, "Strategy Manager - advisor handoff", MANAGER,
                        {"task": "Create advisor_packet using only these specialist outputs.", "context": {"research": research, "financial": financial},
                         "expected_output": "JSON with advisor_packet containing task/context/expected_output."})
    if not isinstance(handoff.get("advisor_packet"), dict):
        raise ValueError("Strategy Manager did not provide an advisor packet.")
    advisor = call_role(ai, model, "Investment Advisor", ADVISOR, handoff["advisor_packet"])
    outputs: dict[str, Any] = {"research": research, "financial": financial, "advisor": advisor}
    qa = call_role(ai, model, "Strategy Manager - quality check", QA,
                   {"task": "Check required report parts and issue at most one repair per role.", "context": outputs,
                    "expected_output": {"repairs": []}})
    specs = {"Research Analyst": ("research", RESEARCH), "Financial Analyst": ("financial", FINANCIAL), "Investment Advisor": ("advisor", ADVISOR)}
    for repair in qa.get("repairs", []) if isinstance(qa.get("repairs", []), list) else []:
        role, packet = repair.get("role"), repair.get("packet")
        if role in specs and isinstance(packet, dict):
            key, instructions = specs[role]
            outputs[key] = call_role(ai, model, f"{role} - repair", instructions, packet)
    report = call_role(ai, model, "Strategy Manager - compile report", REPORT,
                       {"task": "Compile the final report.", "context": outputs, "expected_output": "Markdown with five required H2 sections."}, markdown=True)
    first_line = "; ".join(f"{form} {filings[form]['accessionNumber']}" for form in ("10-K", "10-Q", "8-K"))
    path = destination / "equity_research_report.md"
    path.write_text(f"Filings read: {first_line}; 8-K Exhibit 99.1\nRun time: {datetime.now().astimezone().isoformat(timespec='seconds')}\n\n{report.strip()}\n", encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect SEC filing text and annual XBRL facts for a ticker.")
    parser.add_argument("ticker")
    parser.add_argument("--peers", nargs="+", default=[],
                        help="Peer tickers, separated by spaces and/or commas (for example WMT,TGT,BJ).")
    parser.add_argument("--price", type=float)
    parser.add_argument("--step", type=int, choices=range(1, 8), help="Run only this numbered step (dependencies are fetched silently).")
    parser.add_argument("--report", action="store_true",
                        help="After a full seven-step run, invoke the optional OpenAI model-role report workflow.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # Accept both conventional shell-separated lists and the compact comma form.
    args.peers = [ticker.strip().upper() for value in args.peers for ticker in value.split(",") if ticker.strip()]
    # Always use the project's .env, even if the script is launched elsewhere.
    load_dotenv(Path(__file__).with_name(".env"))
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
        print(f"  Source: 8-K Exhibit 99.1 | accession {filings['8-K']['accessionNumber']}")
        revenue_line, eps_line = release_lines(texts["8-K"])
        print("  Quarterly total revenue: " + revenue_line)
        print("  Diluted EPS: " + eps_line)
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
    # Lab 03 roles are opt-in and can never interrupt a default Lab 02 data run.
    if args.report and one_step is None:
        report_path = run_roles(destination, issuer, filings, texts, facts, calculator(facts, args.price),
                                release_lines(texts["8-K"]), peer_results)
        print(f"Report saved: {report_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, OpenAIError, LookupError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
