"""Fixed, evidence-first OpenAI Agents SDK equity-research graph.

Models never route this graph.  Python executes the deterministic SEC steps,
then the roles in a fixed order, followed by one bounded repair pass and an
always-on verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agents import Agent, Runner, function_tool
from dotenv import load_dotenv

from sec_research import (EdgarClient, acceptance_time, calculator, collect_facts,
    company_facts, download_texts, latest_filings, price_details, release_lines,
    resolve_ticker, save_facts, submissions)

ROOT, ROLES, RUNS = Path(__file__).parent, Path(__file__).parent / "roles", Path(__file__).parent / "agent_runs"
ROLE_ORDER = ("Research Analyst", "Financial Analyst", "Investment Advisor")
PARTS = ("Overview", "Market Positioning and Growth Outlook", "Valuation Insights",
         "Future Developments and Strategic Recommendations", "Investment Recommendation")
ROLE_FILES = {"Research Analyst": "research_analyst.md", "Financial Analyst": "financial_analyst.md",
              "Investment Advisor": "investment_advisor.md", "Strategy Manager": "strategy_manager.md",
              "Verifier": "verifier.md"}
ROLE_PARTS = {
    "Research Analyst": ("Overview", "Market Positioning and Growth Outlook", "Future Developments and Strategic Recommendations"),
    "Financial Analyst": ("Valuation Insights",),
    "Investment Advisor": ("Investment Recommendation",),
}
INSIGHT_SECTIONS = {"Research Analyst": "Overview", "Financial Analyst": "Valuation Insights", "Investment Advisor": "Investment Recommendation"}
INSIGHT_CLAIMS = {"Research Analyst": "Insight-Research", "Financial Analyst": "Insight-Financial", "Investment Advisor": "Insight-Advisor"}


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "issuer"


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _role(name: str) -> str:
    return (ROLES / ROLE_FILES[name]).read_text(encoding="utf-8")


def _tag_insight(node: str, line: str) -> str:
    """Give each Insight a stable verifier-table identifier without changing its evidence."""
    if node not in INSIGHT_CLAIMS or "[line:" not in line.lower() or "|claim=" in line.lower():
        return line
    return re.sub(r"\]$", f"|claim={INSIGHT_CLAIMS[node]}]", line)


def _decode(text: str, node: str) -> dict[str, Any]:
    """Decode a role response and retain its required trailing Insight line."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    insight = re.search(r"(?:^|\n)(Insight:[^\r\n]*)\Z", cleaned)
    if insight:
        cleaned = cleaned[:insight.start()].strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{node} did not return JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{node} must return a JSON object")
    # A missing line is deliberately retained as missing. The Strategy Manager
    # then marks the owning report part failed and can send that role one repair.
    if insight:
        value["_insight_line"] = _tag_insight(node, insight.group(1))
    return value


def _record(run: Path, node: str, attempt: int, packet: dict[str, Any], output: Any | None = None, error: str | None = None) -> None:
    base = run / "nodes" / f"{node.lower().replace(' ', '_')}_{attempt}"
    _json(base.with_name(base.name + "_input.json"), packet)  # Persist before an API request.
    if output is not None:
        _json(base.with_name(base.name + "_output.json"), output)
    if error:
        base.with_name(base.name + "_error.txt").write_text(error, encoding="utf-8")


def _append_run_log(run: Path, node: str, attempt: int, status: str) -> None:
    path = run / "execution.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"order": [], "calls": {}}
    state["order"].append({"node": node, "attempt": attempt, "status": status})
    if status == "started":
        state["calls"][node] = state["calls"].get(node, 0) + 1
    _json(path, state)


def _run_model(run: Path, name: str, packet: dict[str, Any], attempt: int = 1,
               responder: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Exactly one model call (or one labelled offline mock response)."""
    print(name)
    _record(run, name, attempt, packet)
    _append_run_log(run, name, attempt, "started")
    try:
        if responder:
            output = responder(name, packet)
        else:
            agent = Agent(name=name, instructions=_role(name), model=os.getenv("OPENAI_MODEL", "gpt-4.1"))
            output = _decode(str(Runner.run_sync(agent, json.dumps(packet), max_turns=1).final_output), name)
        _record(run, name, attempt, packet, output)
        _append_run_log(run, name, attempt, "completed")
        return output
    except Exception as exc:
        _record(run, name, attempt, packet, error=str(exc))
        _append_run_log(run, name, attempt, "failed")
        raise


# Real deterministic step implementations. SDK wrappers below call these exact
# functions too, rather than returning a placeholder ticker.
def _step_resolve(client: EdgarClient, ticker: str) -> dict[str, str]: return resolve_ticker(client, ticker)
def _step_filings(client: EdgarClient, cik: str) -> dict[str, dict[str, str]]: return latest_filings(submissions(client, cik))
def _step_texts(client: EdgarClient, cik: str, filings: dict[str, Any], out: Path) -> dict[str, str]: return download_texts(client, cik, filings, out)
def _step_facts(client: EdgarClient, cik: str, text: str, filing: dict[str, str]): return collect_facts(company_facts(client, cik), text, filing)
def _step_metrics(facts: dict[str, Any], price: float | None) -> list[str]: return calculator(facts, price)
def _step_release(text: str) -> list[str]: return release_lines(text)
def _step_peer(client: EdgarClient, ticker: str) -> tuple[dict[str, str], tuple[Any, ...]]:
    issuer = resolve_ticker(client, ticker); return issuer, collect_facts(company_facts(client, issuer["cik"]))


@function_tool
def step_1_resolve_ticker(ticker: str, sec_user_agent: str) -> str:
    """Resolve a ticker with SEC EDGAR and return its issuer JSON."""
    return json.dumps(_step_resolve(EdgarClient(sec_user_agent), ticker))
@function_tool
def step_2_select_filings(cik: str, sec_user_agent: str) -> str:
    """Select latest annual, quarterly, and earnings-release filings."""
    return json.dumps(_step_filings(EdgarClient(sec_user_agent), cik))
@function_tool
def step_3_download_filings(cik: str, filings_json: str, output_dir: str, sec_user_agent: str) -> str:
    """Download selected filing text to output_dir."""
    return json.dumps(_step_texts(EdgarClient(sec_user_agent), cik, json.loads(filings_json), Path(output_dir)))
@function_tool
def step_4_collect_facts(cik: str, ten_k_text: str, filing_json: str, sec_user_agent: str) -> str:
    """Collect annual XBRL facts using the selected 10-K."""
    return json.dumps(_step_facts(EdgarClient(sec_user_agent), cik, ten_k_text, json.loads(filing_json)), default=str)
@function_tool
def step_5_calculate_metrics(facts_json: str, price: float | None = None) -> str:
    """Calculate the six prescribed metrics in Python."""
    return json.dumps(_step_metrics(json.loads(facts_json), price))
@function_tool
def step_6_extract_release_lines(earnings_release_text: str) -> str:
    """Extract quarterly revenue and diluted EPS statements."""
    return json.dumps(_step_release(earnings_release_text))
@function_tool
def step_7_collect_peer(peer_ticker: str, sec_user_agent: str) -> str:
    """Collect a peer issuer's annual XBRL facts."""
    issuer, data = _step_peer(EdgarClient(sec_user_agent), peer_ticker); return json.dumps({"issuer": issuer, "data": data}, default=str)
TEAM_TOOLS = (step_1_resolve_ticker, step_2_select_filings, step_3_download_filings, step_4_collect_facts,
              step_5_calculate_metrics, step_6_extract_release_lines, step_7_collect_peer)


def _steps(ticker: str, peers: list[str], price: float | None) -> tuple[Path, dict[str, Any]]:
    print("Steps")
    load_dotenv(ROOT / ".env"); client = EdgarClient(os.getenv("SEC_USER_AGENT", ""))
    issuer = _step_resolve(client, ticker)
    run = RUNS / f"{_safe(issuer['name'])}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"; run.mkdir(parents=True, exist_ok=False)
    packet = {"task": "Run seven deterministic SEC steps in their defined order.", "context": {"ticker": ticker, "peers": peers, "price": price}, "expected_output": "Saved texts, facts, metrics, release lines, and peer facts."}
    _record(run, "Steps", 1, packet); _append_run_log(run, "Steps", 1, "started")
    try:
        filings = _step_filings(client, issuer["cik"]); texts = _step_texts(client, issuer["cik"], filings, run)
        year, end, prior_end, facts = _step_facts(client, issuer["cik"], texts["10-K"], filings["10-K"])
        peer_data: dict[str, Any] = {}
        for peer_ticker in peers:
            peer, values = _step_peer(client, peer_ticker); py, pe, pp, pf = values
            peer_data[peer["ticker"]] = {"ticker": peer["ticker"], "cik": peer["cik"], "fiscal_year": py, "period_end": pe, "prior_period_end": pp, "facts": pf}
        save_facts(run, issuer, year, end, prior_end, facts, peer_data, price_details(price))
        output = {"issuer": issuer, "filings": filings, "acceptance_times": {f: acceptance_time(client, issuer["cik"], x) for f, x in filings.items()}, "metrics": _step_metrics(facts, price), "release_lines": _step_release(texts["8-K"]), "peer_count": len(peer_data)}
        _json(run / "filings.json", filings); _record(run, "Steps", 1, packet, output); _append_run_log(run, "Steps", 1, "completed")
        return run, {"issuer": issuer, "filings": filings, "texts": texts, "facts": facts, "peers": peer_data, "price": price_details(price), **output}
    except Exception as exc:
        _record(run, "Steps", 1, packet, error=str(exc)); _append_run_log(run, "Steps", 1, "failed"); raise


def _evidence(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in ("issuer", "filings", "texts", "facts", "peers", "price", "metrics", "release_lines")}

def _packet(role: str, evidence: dict[str, Any], outputs: dict[str, Any], repair: str | None = None) -> dict[str, Any]:
    task = {"Research Analyst": "Produce qualitative research from evidence only.", "Financial Analyst": "Use supplied Python calculations; do not calculate in the model.", "Investment Advisor": "Synthesize only the Research and Financial Analyst outputs."}[role]
    # The Advisor is a hard boundary: it sees the two analysts' work and no
    # raw filings, facts, price data, ratios, or peer data, including on repair.
    context: dict[str, Any]
    if role == "Investment Advisor":
        context = {"analyst_outputs": {x: outputs[x] for x in ("Research Analyst", "Financial Analyst")}}
    else:
        context = {"evidence": evidence}
    if repair: context["repair_request"] = repair
    return {"task": task, "context": context, "expected_output": "JSON using the role file's requested fields and source citations, followed by exactly one final line: Insight: CLAIM [line: filing=FORM|quote=VERBATIM FILING LINE]. The Insight must make one filing-supported claim beyond merely restating a figure in the earnings release opening paragraph. Put no text after it."}


def _opening_release_paragraph(release: str) -> str:
    """Extract the first narrative paragraph from the saved Exhibit 99.1 text."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in release.splitlines()]
    for line in lines:
        if len(line) > 60 and re.search(r"\b(today\s+(?:announced|reported)|announced\s+(?:its|the)|reported\s+(?:its|the))\b", line, re.I):
            return line
    return next((line for line in lines if len(line) > 60 and not line.isupper()), "")


def _insight_reason(line: Any, opening_paragraph: str) -> str | None:
    if not isinstance(line, str) or not re.fullmatch(r"Insight:\s*.+\[line:\s*filing=(?:10-K|10-Q|8-K)\|quote=[^\]]+\]", line.strip(), re.I):
        return "missing a final Insight line with a filing and exact quoted filing line"
    claim = line.split("[line:", 1)[0]
    claimed_numbers = re.findall(r"(?<![A-Za-z])\$?[\d,]+(?:\.\d+)?%?", claim)
    opening_numbers = re.findall(r"(?<![A-Za-z])\$?[\d,]+(?:\.\d+)?%?", opening_paragraph)
    if claimed_numbers and all(number in opening_numbers for number in claimed_numbers):
        remainder = re.sub(r"(?<![A-Za-z])\$?[\d,]+(?:\.\d+)?%?", "", claim.lower())
        remainder = re.sub(r"\b(insight|revenue|sales|earnings|eps|diluted|net|income|was|were|is|of|the|a|an|for|quarter|year)\b", "", remainder)
        if not re.sub(r"[^a-z]", "", remainder):
            return "only restates a figure from the saved Exhibit 99.1 opening paragraph"
    return None


def _insight_checks(evidence: dict[str, Any], outputs: dict[str, Any]) -> list[dict[str, str]]:
    opening = _opening_release_paragraph(str(evidence.get("texts", {}).get("8-K", "")))
    checks: list[dict[str, str]] = []
    for role, parts in ROLE_PARTS.items():
        reason = _insight_reason(outputs.get(role, {}).get("_insight_line"), opening)
        if reason:
            checks.extend({"status": "fail", "role": role, "part": part, "reason": reason} for part in parts)
    return checks

def _manager_packet(evidence: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    opening = _opening_release_paragraph(str(evidence.get("texts", {}).get("8-K", "")))
    return {"task": "Check required parts and compile an evidence-cited report.", "context": {"evidence": evidence, "role_outputs": outputs, "required_parts": PARTS, "insight_requirements": {"required_prefix": "Insight:", "required_source": "filing and verbatim source line", "opening_paragraph": opening, "reject_release_first_paragraph_restatement": True}}, "expected_output": "JSON checks and exactly five H2 report sections."}


def _apply_insight_checks(manager: dict[str, Any], evidence: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    checks = manager.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    forced = _insight_checks(evidence, outputs)
    existing = {(str(check.get("role")), str(check.get("part")), str(check.get("reason"))) for check in checks if isinstance(check, dict)}
    return {**manager, "checks": checks + [check for check in forced if (check["role"], check["part"], check["reason"]) not in existing]}

def _repairs(manager: dict[str, Any]) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    for check in manager.get("checks", []):
        if isinstance(check, dict) and str(check.get("status")).lower() == "fail" and check.get("role") in ROLE_ORDER:
            result.setdefault(check["role"], []).append(f"{check.get('part')}: {check.get('reason', 'failed')}")
    return {role: "; ".join(result[role]) for role in ROLE_ORDER if role in result}  # fixed repair order

def _filings_line(filings: dict[str, Any]) -> str:
    return "Filings read: " + "; ".join(f"{form} {filings[form]['accessionNumber']}" for form in ("10-K", "10-Q", "8-K"))

def _report(manager: dict[str, Any], filings: dict[str, Any], outputs: dict[str, Any]) -> str:
    body = str(manager.get("report", "")).strip(); headings = re.findall(r"^##[ \t]+(.+?)[ \t]*$", body, re.M)
    if headings != list(PARTS): raise ValueError("Manager did not compile exactly the five required sections")
    # Render each role's retained Insight exactly once, even if the manager
    # drafted an extra copy while compiling the report.
    body = re.sub(r"^Insight:[^\r\n]*(?:\r?\n|$)", "", body, flags=re.M).strip()
    for role, section in INSIGHT_SECTIONS.items():
        insight = outputs.get(role, {}).get("_insight_line")
        if not insight:
            continue
        start = re.search(rf"^##\s+{re.escape(section)}\s*$", body, re.M)
        if not start:
            continue
        next_heading = re.search(r"^##\s+", body[start.end():], re.M)
        end = start.end() + next_heading.start() if next_heading else len(body)
        if insight not in body[start.start():end]:
            body = body[:end].rstrip() + "\n\n" + insight + "\n" + body[end:]
    return _filings_line(filings) + "\nRun time: " + datetime.now().astimezone().isoformat(timespec="seconds") + "\n\n" + body + "\n"

# --- Verification ---------------------------------------------------------
def _num(text: str) -> tuple[Decimal, bool]:
    """Parse report notation without losing a leading or Unicode minus sign."""
    raw = text.strip().replace("−", "-").replace("$", "").replace(",", "")
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()"); percent = raw.endswith("%")
    try: value = Decimal(raw.rstrip("%"))
    except InvalidOperation as exc: raise ValueError(f"Not a number: {text}") from exc
    return (-value if negative else value), percent
def _norm(text: str) -> str: return re.sub(r"\s+", " ", text).strip().lower()
def _claim_id(text: str, pos: int) -> str: return "C" + hashlib.sha1(f"{pos}:{text[pos:pos+80]}".encode()).hexdigest()[:10]
def _section_at(report: str, pos: int) -> str:
    prior = list(re.finditer(r"^##\s+(.+)$", report[:pos], re.M)); return prior[-1].group(1).strip() if prior else ""

def _fact_payload(saved: dict[str, Any], company: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    issuer = saved.get("issuer", {})
    if company.lower() in {"issuer", str(issuer.get("ticker", "")).lower(), str(issuer.get("cik", "")).lower()}:
        return issuer, issuer.get("facts")
    for key, peer in saved.get("peers", {}).items():
        if company.lower() in {key.lower(), str(peer.get("ticker", "")).lower(), str(peer.get("cik", "")).lower()}:
            return peer, peer.get("facts")
    return None, None

def _fact_check(number: str, citation: str, saved: dict[str, Any]) -> dict[str, Any]:
    fields = dict(re.findall(r"(company|fact|period|tag|unit|scale|accn|claim)=([^|\]]+)", citation, re.I))
    owner, facts = _fact_payload(saved, fields.get("company", "issuer")); label, period = fields.get("fact"), fields.get("period")
    claim = {"id": fields.get("claim", ""), "reference": number + " [xbrl:" + citation + "]", "status": "unsupported", "replacement": "Claim remains unsupported: citation cannot be resolved."}
    item = facts.get(label) if facts and label else None; value = item.get(period) if item and period in {"current", "prior"} else None
    if not owner or not item or not value: return claim
    tag = item.get("tag")  # Actual schema: tag lives beside current/prior.
    expected_unit = "USD/shares" if label == "Diluted EPS" else "USD"
    required = (fields.get("tag") == str(tag) and fields.get("accn") == str(value.get("accn")) and fields.get("unit", expected_unit) == expected_unit)
    reported, pct = _num(number); reported *= Decimal(fields.get("scale", "1")); actual = Decimal(str(value["val"]))
    if pct: required = False
    claim.update({"company": owner.get("ticker"), "period": value.get("end"), "source": f"[xbrl: company={owner.get('ticker')}|fact={label}|period={period}|tag={tag}|unit={expected_unit}|scale=1|accn={value.get('accn')}]"})
    if not required: return claim
    claim["status"] = "correct" if reported == actual else "wrong"; claim["replacement"] = f"{actual.normalize():f} {claim['source']}"; return claim

def _line_check(number: str, citation: str, texts: dict[str, str]) -> dict[str, Any]:
    fields = dict(re.findall(r"(filing|quote|claim)=([^|\]]+)", citation, re.I)); filing, quote = fields.get("filing", ""), fields.get("quote", "")
    source = texts.get(filing, ""); present = bool(source) and _norm(quote) in _norm(source); num_present = _norm(number) in _norm(quote)
    return {"id": fields.get("claim", ""), "reference": number + " [line:" + citation + "]", "status": "correct" if present and num_present else ("wrong" if present else "unsupported"), "replacement": f"{number} [line: filing={filing}|quote={quote}]" if present and num_present else (f"[line: filing={filing}|quote={quote}]" if present else "Claim remains unsupported: saved filing line is absent.")}

def _per_share_line(texts: dict[str, str], value: str) -> str | None:
    """Return an actual saved 10-K diluted-per-share line when one is available."""
    forms = {value, value.rstrip("0").rstrip(".")}
    for line in texts.get("10-K", "").splitlines():
        normal = _norm(line)
        if "diluted" in normal and "per" in normal and "share" in normal and any(form in normal for form in forms):
            return line.strip()
    return None


def _formula(name: str, facts: dict[str, Any], price: float | None) -> tuple[Decimal | None, str]:
    def v(label: str, period: str = "current") -> Decimal | None:
        item = facts.get(label) or {}; x = item.get(period); return Decimal(str(x["val"])) if x and x.get("val") is not None else None
    rev, prev = v("Total revenue"), v("Total revenue", "prior"); op, ni = v("Operating income"), v("Net income"); eq, peq = v("Stockholders' equity"), v("Stockholders' equity", "prior"); debt, cur = v("Long-term debt"), v("Current portion of long-term debt"); cfo, capex, eps = v("Cash from operations"), v("Additions to property and equipment"), v("Diluted EPS")
    vals = {"Revenue growth": None if rev is None or prev in (None, Decimal(0)) else (rev-prev)/prev, "Operating margin": None if op is None or rev in (None, Decimal(0)) else op/rev, "ROE": None if ni is None or eq is None or peq is None else ni/((eq+peq)/2), "Debt/equity": None if debt is None or cur is None or eq in (None, Decimal(0)) else (debt+cur)/eq, "FCF": None if cfo is None or capex is None else cfo-capex, "P/E": None if price is None or eps in (None, Decimal(0)) else Decimal(str(price))/eps}
    return vals.get(name), name

def _formula_inputs(name: str, facts: dict[str, Any], price: Any) -> list[Decimal] | None:
    def v(label: str, period: str = "current") -> Decimal | None:
        row = (facts.get(label) or {}).get(period); return Decimal(str(row["val"])) if row and row.get("val") is not None else None
    mapping = {"Revenue growth": [v("Total revenue"), v("Total revenue", "prior")], "Operating margin": [v("Operating income"), v("Total revenue")], "ROE": [v("Net income"), v("Stockholders' equity", "prior"), v("Stockholders' equity")], "Debt/equity": [v("Long-term debt"), v("Current portion of long-term debt"), v("Stockholders' equity")], "FCF": [v("Cash from operations"), v("Additions to property and equipment")], "P/E": [Decimal(str(price)) if price is not None else None, v("Diluted EPS")]}
    values = mapping[name]; return None if any(x is None for x in values) else values

def _display_matches(actual: Decimal, shown: Decimal, percent: bool) -> bool:
    target = actual * Decimal(100) if percent else actual
    places = max(0, -shown.as_tuple().exponent)
    return target.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) == shown

def _ratio_check(report: str, saved: dict[str, Any]) -> list[dict[str, Any]]:
    patterns = {"Revenue growth": r"Revenue growth[^\n]*", "Operating margin": r"Operating margin[^\n]*", "ROE": r"(?:ROE|Return on average equity)[^\n]*", "Debt/equity": r"Debt[^\n]*equity[^\n]*", "FCF": r"(?:FCF|Free cash flow)[^\n]*", "P/E": r"P/E[^\n]*"}; marks=[]
    for name, pat in patterns.items():
        for m in re.finditer(pat, report, re.I):
            line=m.group(0); citation=re.search(r"\[formula:\s*([^\]]+)\]", line, re.I)
            if not citation: continue
            fields=dict(re.findall(r"(definition|company|inputs|claim)=([^|\]]+)", citation.group(1), re.I)); owner,facts=_fact_payload(saved,fields.get("company","issuer")); claim_id=fields.get("claim") or _claim_id(report,m.start())
            expression=line[:line.find("=")] if "=" in line else line[:citation.start()]
            nums=[_num(x)[0] for x in re.findall(r"(?<![A-Za-z])[-−]?\(?\$?[\d,]+(?:\.\d+)?\)?", expression)]
            result_match=re.search(r"=\s*([-−]?\(?\$?[\d,]+(?:\.\d+)?\)?)(%)?", line); shown=_num(result_match.group(1)+(result_match.group(2) or "")) if result_match else None
            inputs=_formula_inputs(name,facts or {},saved.get("price",{}).get("price") if owner is saved.get("issuer") else None)
            expected,_=_formula(name,facts or {},saved.get("price",{}).get("price") if owner is saved.get("issuer") else None)
            quoted = [nums[0], nums[-1]] if name == "Revenue growth" and len(nums) >= 2 else nums[-len(inputs):] if inputs and len(nums) >= len(inputs) else []
            quoted_ok = inputs is not None and quoted == inputs
            defined = owner is not None and fields.get("definition","").lower()==name.lower() and bool(fields.get("inputs"))
            status="unsupported"; replacement="Claim remains unsupported: formula definition, owner, or sourced inputs are incomplete."
            if defined and inputs is not None and expected is not None:
                replacement=(f"{expected*100:.2f}%" if shown and shown[1] else f"{expected:,.2f}") + f" [formula: definition={name}|company={owner.get('ticker')}|inputs={fields.get('inputs')}|claim={claim_id}]"
                if not quoted_ok: status="wrong"; replacement="Correct inputs: " + ", ".join(f"{x:f}" for x in inputs) + "; " + replacement
                elif shown and _display_matches(expected,shown[0],shown[1]): status="correct"
                else: status="wrong"
            marks.append({"id":claim_id,"reference":line,"start":m.start(),"end":m.end(),"status":status,"replacement":replacement,"company":owner.get("ticker") if owner else None,"source":f"formula {name}"})
    return marks


# Normalize both ASCII and Unicode financial notation before every verifier
# comparison.  A leading minus is significant; zero remains a usable value.
def _num(text: str) -> tuple[Decimal, bool]:
    raw = text.strip().replace("\N{MINUS SIGN}", "-").replace("$", "").replace(",", "")
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    percent = raw.endswith("%")
    try:
        value = Decimal(raw.rstrip("%"))
    except InvalidOperation as exc:
        raise ValueError(f"Not a number: {text}") from exc
    return (-value if negative else value), percent


# This definition replaces the early draft above. It recognizes the intermediate
# values printed by the Step 5 Python calculator without weakening the check.
def _ratio_check(report: str, saved: dict[str, Any]) -> list[dict[str, Any]]:
    patterns = {
        "Revenue growth": r"Revenue growth[^\n]*", "Operating margin": r"Operating margin[^\n]*",
        "ROE": r"(?:ROE|Return on average equity)[^\n]*",
        "Debt/equity": r"Debt[^\n]*equity[^\n]*", "FCF": r"(?:FCF|Free cash flow)[^\n]*",
        "P/E": r"P/E[^\n]*",
    }
    token = r"(?:\([-\u2212]?\$?[\d,]+(?:\.\d+)?\)|[-\u2212]?\$?[\d,]+(?:\.\d+)?%?)"
    marks: list[dict[str, Any]] = []
    for name, pattern in patterns.items():
        for match in re.finditer(pattern, report, re.I):
            line = match.group(0)
            citation = re.search(r"\[formula:\s*([^\]]+)\]", line, re.I)
            if not citation:
                continue
            fields = dict(re.findall(r"(definition|company|inputs|claim)=([^|\]]+)", citation.group(1), re.I))
            owner, facts = _fact_payload(saved, fields.get("company", "issuer"))
            claim_id = fields.get("claim") or _claim_id(report, match.start())
            arithmetic = line[:line.find("=")].split(":", 1)[-1] if "=" in line else line[:citation.start()]
            quoted: list[Decimal] = []
            for raw in re.findall(token, arithmetic):
                try:
                    quoted.append(_num(raw)[0])
                except ValueError:
                    pass
            result = re.search(r"=\s*(\([-\u2212]?\$?[\d,]+(?:\.\d+)?\)|[-\u2212]?\$?[\d,]+(?:\.\d+)?)(%)?", line)
            shown = _num(result.group(1) + (result.group(2) or "")) if result else None
            price = saved.get("price", {}).get("price") if owner is saved.get("issuer") else None
            inputs = _formula_inputs(name, facts or {}, price)
            expected, _ = _formula(name, facts or {}, price)
            quoted_ok = False
            quoted_value: Decimal | None = None
            if inputs is not None:
                if name == "Revenue growth" and len(quoted) >= 3:
                    # Check the formula as printed: (current - prior) / prior.
                    # A plus sign or missing grouping is not the same formula,
                    # even when a report happens to display the canonical result.
                    number_token = r"[-\u2212]?\$?[\d,]+(?:\.\d+)?"
                    grouped_subtraction = re.fullmatch(
                        rf"\s*\(\s*{number_token}\s*[-\u2212]\s*{number_token}\s*\)\s*/\s*{number_token}\s*",
                        arithmetic,
                    )
                    quoted_ok = bool(grouped_subtraction) and quoted[0] == inputs[0] and quoted[1] == inputs[1] and quoted[2] == inputs[1]
                    if grouped_subtraction and quoted[2] != 0:
                        quoted_value = (quoted[0] - quoted[1]) / quoted[2]
                elif name == "Operating margin" and len(quoted) >= 2:
                    quoted_ok = quoted[-2:] == inputs
                    if quoted[-1] != 0:
                        quoted_value = quoted[-2] / quoted[-1]
                elif name == "ROE" and len(quoted) >= 2:
                    average = (inputs[1] + inputs[2]) / Decimal(2)
                    quoted_ok = (quoted[-3:] == inputs) if len(quoted) >= 3 else (quoted[-2] == inputs[0] and quoted[-1] == average)
                    if quoted[-1] != 0:
                        quoted_value = quoted[-2] / quoted[-1]
                elif name == "Debt/equity" and len(quoted) >= 2:
                    total_debt = inputs[0] + inputs[1]
                    quoted_ok = (quoted[-3:] == inputs) if len(quoted) >= 3 else (quoted[-2] == total_debt and quoted[-1] == inputs[2])
                    if quoted[-1] != 0:
                        quoted_value = quoted[-2] / quoted[-1]
                elif name == "FCF" and len(quoted) >= 2:
                    quoted_ok = quoted[-2:] == inputs
                    quoted_value = quoted[-2] - quoted[-1]
                elif name == "P/E" and len(quoted) >= 2:
                    quoted_ok = quoted[-2:] == inputs
                    if quoted[-1] != 0:
                        quoted_value = quoted[-2] / quoted[-1]
            defined = owner is not None and fields.get("definition", "").lower() == name.lower() and bool(fields.get("inputs"))
            status = "unsupported"
            replacement = "Claim remains unsupported: formula definition, owner, or sourced inputs are incomplete."
            if defined and inputs is not None and expected is not None:
                replacement = (f"{expected * 100:.2f}%" if shown and shown[1] else f"{expected:,.2f}") + f" [formula: definition={name}|company={owner.get('ticker')}|inputs={fields.get('inputs')}|claim={claim_id}]"
                if not quoted_ok:
                    status = "wrong"
                    replacement = "Correct inputs: " + ", ".join(f"{value:f}" for value in inputs) + "; " + replacement
                elif quoted_value is not None and shown and _display_matches(quoted_value, shown[0], shown[1]) and _display_matches(expected, shown[0], shown[1]):
                    status = "correct"
                else:
                    status = "wrong"
            marks.append({"id": claim_id, "reference": line, "original": line, "start": match.start(), "end": match.end(), "status": status, "replacement": replacement, "company": owner.get("ticker") if owner else None, "source": f"formula {name}"})
    return marks


def _fact_number(facts: dict[str, Any], label: str, period: str) -> Decimal | None:
    row = (facts.get(label) or {}).get(period)
    return Decimal(str(row["val"])) if row and row.get("val") is not None else None


def _eps_corrections(report: str, saved: dict[str, Any], texts: dict[str, str]) -> list[dict[str, Any]]:
    """Correct a diluted-EPS claim even when its report quote was altered too."""
    issuer = saved.get("issuer", {})
    facts = issuer.get("facts", {})
    item = facts.get("Diluted EPS") or {}
    if not item:
        return []
    marks: list[dict[str, Any]] = []
    # Include an altered [line: ...] citation in the replaced span so the stale
    # quotation cannot survive beside the corrected EPS.
    pattern = r"(?:diluted\s+(?:eps|earnings per share)|eps)(?:\s+(?:was|is|of)\s*|\s*[:=]\s*|\s+)(?P<n>[-\u2212]?\$?[\d,]+(?:\.\d+)?)\s*(?:\[line:\s*[^\]]+\])?"
    for match in re.finditer(pattern, report, re.I):
        if "[line:" not in match.group(0).lower():
            # XBRL-only citations are checked by _fact_check, which already
            # carries an explicit current/prior period.
            continue
        try:
            reported, percent = _num(match.group("n"))
        except ValueError:
            continue
        if percent:
            continue
        # Select exactly one saved annual period from the fiscal year stated in
        # the claim or its quoted line. Never silently choose the current year.
        claim_text = report[max(0, match.start() - 80):match.end()]
        prefix = report[max(0, match.start() - 80):match.start()]
        years = {int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", claim_text)}
        current_row, prior_row = item.get("current"), item.get("prior")
        current_years = {int(str((current_row or {}).get("end", ""))[:4])}
        if issuer.get("fiscal_year"):
            current_years.add(int(issuer["fiscal_year"]))
        prior_years = {int(str((prior_row or {}).get("end", ""))[:4])} if prior_row else set()
        prefix_years = {int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", prefix)}
        selected: tuple[str, dict[str, Any]] | None = None
        if current_row and prefix_years.intersection(current_years):
            selected = ("current", current_row)
        elif prior_row and prefix_years.intersection(prior_years):
            selected = ("prior", prior_row)
        elif current_row and years.intersection(current_years):
            selected = ("current", current_row)
        elif prior_row and years.intersection(prior_years) and "compared to" not in claim_text.lower():
            selected = ("prior", prior_row)
        elif current_row and prior_row and years.intersection(prior_years) and "compared to" in claim_text.lower():
            # In a current-year sentence, "compared to ... in 2025" identifies
            # the comparison period, not the first stated EPS.
            selected = ("current", current_row)
        if selected is None:
            marks.append({
                "id": _claim_id(report, match.start("n")), "reference": report[match.start("n"):match.end()],
                "original": report[match.start("n"):match.end()], "start": match.start("n"), "end": match.end(),
                "location": f"offset:{match.start('n')}", "status": "unsupported", "company": issuer.get("ticker"),
                "source": None,
                "replacement": "Claim remains unsupported: the saved evidence does not establish one fiscal period for this EPS claim.",
            })
            continue
        period, value = selected
        actual_value = Decimal(str(value["val"]))
        if reported == actual_value:
            continue
        actual = f"{actual_value.normalize():f}"
        line = _per_share_line(texts, actual)
        if not line:
            marks.append({
                "id": _claim_id(report, match.start("n")), "reference": report[match.start("n"):match.end()],
                "original": report[match.start("n"):match.end()], "start": match.start("n"), "end": match.end(),
                "location": f"offset:{match.start('n')}", "status": "unsupported", "company": issuer.get("ticker"),
                "period": value.get("end"), "source": None,
                "replacement": "Claim remains unsupported: no saved 10-K per-share line establishes the replacement.",
            })
            continue
        source = f"[line: filing=10-K|quote={line}] [xbrl: company={issuer.get('ticker')}|fact=Diluted EPS|period={period}|tag={item.get('tag')}|unit=USD/shares|scale=1|accn={value.get('accn')}]"
        marks.append({
            "id": _claim_id(report, match.start("n")), "reference": report[match.start("n"):match.end()],
            "original": report[match.start("n"):match.end()], "start": match.start("n"), "end": match.end(),
            "location": f"offset:{match.start('n')}", "status": "wrong", "company": issuer.get("ticker"),
            "period": value.get("end"), "source": source,
            "replacement": f"{actual} {source}",
        })
    return marks


def _quoted_growth_checks(report: str, saved: dict[str, Any]) -> list[dict[str, Any]]:
    """Check prose growth claims that quote both revenue figures but no formula tag."""
    issuer = saved.get("issuer", {})
    facts = issuer.get("facts", {})
    current = _fact_number(facts, "Total revenue", "current")
    prior = _fact_number(facts, "Total revenue", "prior")
    item = facts.get("Total revenue") or {}
    if current is None or prior in (None, Decimal(0)):
        return []
    actual = (current - prior) / prior
    tags = f"[xbrl: company={issuer.get('ticker')}|fact=Total revenue|period=current|tag={item.get('tag')}|accn={(item.get('current') or {}).get('accn')}]"
    tags += f" [xbrl: company={issuer.get('ticker')}|fact=Total revenue|period=prior|tag={item.get('tag')}|accn={(item.get('prior') or {}).get('accn')}]"
    marks: list[dict[str, Any]] = []
    for match in re.finditer(r"Revenue growth[^\n]*", report, re.I):
        line = match.group(0)
        if "[formula:" in line:
            continue
        shown = re.search(r"([-\u2212]?\d+(?:\.\d+)?)\s*%", line)
        if not shown:
            continue
        operands: list[tuple[Decimal, Decimal]] = []
        for number in re.finditer(r"(?P<currency>\$)?(?P<value>[\d,]+(?:\.\d+)?)(?:\s*(?P<scale>million|billion|thousand))?", line, re.I):
            # Do not interpret the digits of the percentage as a revenue input.
            if number.start() == shown.start():
                continue
            # Ignore filing labels (10-K) and fiscal-year metadata. A prose
            # revenue input must be explicitly monetary/scaled or match a
            # saved annual revenue value directly.
            raw = number.group("value").strip(",").replace(",", "")
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                continue
            scale = (number.group("scale") or "").lower()
            if not number.group("currency") and not scale and amount not in {current, prior}:
                continue
            normalized = amount * {"thousand": Decimal("1000"), "million": Decimal("1000000"), "billion": Decimal("1000000000")}.get(scale, Decimal(1))
            operands.append((amount, normalized))
        if len(operands) < 2:
            continue
        shown_current, quoted_current = operands[0]
        shown_prior, quoted_prior = operands[1]
        if quoted_current != current or quoted_prior != prior:
            status = "unsupported"
            detail = "Quoted revenue inputs do not match the saved 10-K facts."
        else:
            recomputed = (quoted_current - quoted_prior) / quoted_prior
            status = "correct" if _display_matches(recomputed, Decimal(shown.group(1)), True) else "wrong"
            detail = ""
        arithmetic = f"({shown_current:,.0f} - {shown_prior:,.0f}) / {shown_prior:,.0f} = {actual * Decimal(100):.2f}%"
        replacement = f"Revenue growth: {arithmetic} {tags}"
        if detail:
            replacement = detail + " Correct arithmetic from saved facts: " + f"({current:f} - {prior:f}) / {prior:f} = {actual * Decimal(100):.2f}% {tags}"
        marks.append({"id": _claim_id(report, match.start()), "reference": line, "original": line, "start": match.start(), "end": match.end(), "location": f"offset:{match.start()}", "status": status, "company": issuer.get("ticker"), "source": tags, "replacement": replacement})
    return marks


def _insight_number_checks(report: str, texts: dict[str, str]) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    """Check every written amount on an Insight line against its line citation."""
    amount = r"(?<![A-Za-z])[-−]?\(?\$?[\d,]+(?:\.\d+)?\)?%?(?:\s+(?:thousand|million|billion))?"
    marks: list[dict[str, Any]] = []
    covered: list[tuple[int, int]] = []
    for match in re.finditer(r"^Insight:[^\r\n]*?\[line:\s*(?P<c>[^\]]+)\]", report, re.I | re.M):
        citation_start = report.rfind("[line:", match.start(), match.start("c"))
        prefix = report[match.start():citation_start]
        numbers = list(re.finditer(amount, prefix, re.I))
        if not numbers:
            continue
        claim = re.search(r"claim=([^|\]]+)", match.group("c"), re.I)
        base_id = claim.group(1) if claim else _claim_id(report, match.start())
        for index, number in enumerate(numbers, start=1):
            mark = _line_check(number.group(0), match.group("c"), texts)
            mark["id"] = base_id if len(numbers) == 1 else f"{base_id}-{index}"
            mark.update({"start": None, "end": None, "original": number.group(0), "location": f"offset:{match.start() + number.start()}"})
            marks.append(mark)
        covered.append(match.span())
    return marks, covered


def _code_findings(report: str, saved: dict[str, Any], texts: dict[str, str]) -> list[dict[str, Any]]:
    insight_marks, insight_covered = _insight_number_checks(report, texts)
    marks = _eps_corrections(report, saved, texts) + _quoted_growth_checks(report, saved) + insight_marks
    covered = [(mark["start"], mark["end"]) for mark in marks if mark.get("start") is not None] + insight_covered
    citation_patterns = (
        ("xbrl", lambda n, c: _fact_check(n, c, saved), r"(?P<n>[-−]?\(?\$?[\d,]+(?:\.\d+)?\)?%?)\s*\[xbrl:\s*(?P<c>[^\]]+)\]"),
        # Keep a written scale attached to its source. "$93.9 billion" is one
        # cited amount, not "$93.9" followed by uncited prose.
        ("line", lambda n, c: _line_check(n, c, texts), r"(?P<n>[-−]?\(?\$?[\d,]+(?:\.\d+)?\)?%?(?:\s+(?:thousand|million|billion))?)\s*\[line:\s*(?P<c>[^\]]+)\]"),
    )
    for kind, checker, pattern in citation_patterns:
        for m in re.finditer(pattern, report, re.I):
            if any(start <= m.start("n") < end for start, end in covered):
                continue
            mark=checker(m.group("n"),m.group("c")); mark["id"] = mark["id"] or _claim_id(report,m.start()); mark.update({"start":m.start(),"end":m.end(),"original":m.group(0),"location":f"offset:{m.start()}"}); marks.append(mark); covered.append(m.span())
            fields = dict(re.findall(r"(fact|period)=([^|\]]+)", m.group("c"), re.I))
            if kind == "xbrl" and fields.get("fact") == "Diluted EPS" and mark.get("status") == "wrong":
                actual = str(mark.get("replacement", "")).split(" [", 1)[0]
                line = _per_share_line(texts, actual)
                if line:
                    source = f" [line: filing=10-K|quote={line}]"
                    mark["source"] = str(mark.get("source", "")) + source
                    mark["replacement"] = str(mark["replacement"]) + source
    marks.extend(_ratio_check(report, saved))
    covered += [(x["start"], x["end"]) for x in marks if x.get("start") is not None]
    # Financial claim language prevents dates/accession numbers/list labels from becoming claims.
    pattern=r"(?:revenue|sales|income|eps|earnings per share|margin|equity|debt|cash flow|capex|price)\D{0,40}(?P<n>[-−]?\$?\(?[\d,]+(?:\.\d+)?\)?%?)"
    for m in re.finditer(pattern, report, re.I):
        ns=m.start("n"); ne=m.end("n")
        # In prose such as "Revenue in fiscal 2026 was $999999", the year
        # supplies context; the following amount is the financial claim.
        if re.fullmatch(r"(?:19|20)\d{2}", m.group("n")):
            following = re.search(r"[-\u2212]?\$?\(?[\d,]+(?:\.\d+)?\)?%?", report[ne:min(len(report), ne + 40)])
            if following:
                ns = ne + following.start()
                ne = ne + following.end()
        if any(a <= ns < b for a,b in covered): continue
        marks.append({"id":_claim_id(report,ns),"reference":report[m.start():ne],"original":report[ns:ne],"start":ns,"end":ne,"location":f"offset:{ns}","status":"unsupported","replacement":"Claim remains unsupported: no valid saved-fact, filing-line, or formula citation.","source":None})
    ids={}
    for mark in marks:
        ids.setdefault(mark["id"],[]).append(mark)
    for same in ids.values():
        if len(same)>1:
            for mark in same: mark["status"]="unsupported"; mark["replacement"]="Claim remains unsupported: duplicate claim identifier."
    return marks

def _reconcile(code: list[dict[str, Any]], semantic: dict[str, Any]) -> list[dict[str, Any]]:
    checks=semantic.get("semantic_checks",[])
    if not isinstance(checks,list): raise ValueError("Verifier semantic_checks must be a list")
    by_id={}; by_ref={}
    for x in checks:
        if not isinstance(x,dict) or not all(k in x for k in ("reference","status","reason")): raise ValueError("Verifier finding requires reference, status, reason")
        if x.get("claim_id"): by_id.setdefault(str(x["claim_id"]),[]).append(x)
        by_ref.setdefault(str(x["reference"]),[]).append(x)
    final=[]; seen=set()
    for mark in code:
        candidates=by_id.get(str(mark["id"]),[]) or by_ref.get(str(mark.get("reference")),[])
        finding=candidates[0] if len(candidates)==1 else None; status=mark["status"]
        if len(candidates)>1: status="unsupported"; mark={**mark,"replacement":"Claim remains unsupported: ambiguous semantic match."}
        if finding and str(finding.get("status")).lower() in {"wrong","unsupported"}:
            status=str(finding["status"]).lower()
            if mark["status"] == "correct":
                mark = {**mark, "replacement": "Claim remains unsupported: " + str(finding.get("reason", "semantic evidence rejected the claim."))}
        # A semantic model cannot upgrade exact code conflict/unsupported evidence.
        final.append({**mark,"semantic":finding,"final_status":status}); seen.add(str(mark["id"]))
    for ident, findings in by_id.items():
        if ident not in seen:
            finding=findings[0]; final.append({"id":ident,"reference":finding["reference"],"original":finding["reference"],"start":None,"end":None,"status":"unsupported","final_status":str(finding["status"]).lower(),"replacement":"Claim remains unsupported: semantic-only finding has no unambiguous report span.","semantic":finding,"source":None})
    for reference, findings in by_ref.items():
        if not any(str(m.get("reference")) == reference for m in final):
            finding=findings[0]; final.append({"id":_claim_id(reference,0),"reference":reference,"original":reference,"start":None,"end":None,"status":"unsupported","final_status":str(finding["status"]).lower(),"replacement":"Claim remains unsupported: semantic-only finding has no unambiguous report span.","semantic":finding,"source":None})
    return final

def _correct_report(report: str, marks: list[dict[str, Any]]) -> str:
    corrected=report
    for mark in sorted(marks,key=lambda x:x.get("start") if x.get("start") is not None else -1,reverse=True):
        if mark["final_status"] == "correct" or not mark.get("replacement"): continue
        if mark.get("start") is None: continue
        start,end=mark["start"],mark["end"]
        if corrected[start:end] != mark.get("original",mark.get("reference")): continue
        if "remains unsupported" in mark["replacement"]:
            corrected=corrected[:end] + " **Verification: " + mark["replacement"] + "**" + corrected[end:]
            continue
        corrected=corrected[:start] + mark["replacement"] + corrected[end:]
    first, rest=corrected.split("\n",1); rows=["| Claim | Mark | Supported replacement |","|---|---|---|"]
    for m in marks: rows.append(f"| {m['id']} | {m['final_status']} | {str(m.get('replacement','Claim remains unsupported.')).replace('|','\\|')} |")
    # The table belongs immediately below the unchanged filings line.
    return first+"\n"+"\n".join(rows)+"\n\n"+rest.lstrip()

def _verify(run: Path, report: str, saved: dict[str, Any], texts: dict[str, str], responder=None) -> str:
    code=_code_findings(report,saved,texts)
    packet={"task":"Semantically check each factual numeric claim. Do not assess the subjective thesis, but do check factual numbers in Investment Recommendation and every Insight line.","context":{"compiled_report":report,"saved_filing_texts":texts,"saved_facts":saved},"expected_output":"JSON semantic_checks keyed by claim id or exact report location, with correct/wrong/unsupported and reason."}
    stamp=datetime.now().strftime("%Y%m%d_%H%M%S_%f"); root=run/"verification_attempts"; attempt=root/stamp; suffix=1
    while attempt.exists():
        attempt=root/f"{stamp}_{suffix}"; suffix += 1
    attempt.mkdir(parents=True,exist_ok=False)
    _json(attempt/"code_findings.json",code)
    try:
        semantic=_run_model(attempt,"Verifier",packet,1,responder); final=_reconcile(code,semantic)
        _json(attempt/"semantic_findings.json",semantic); _json(attempt/"reconciled_marks.json",final)
        verified=_correct_report(report,final); target=attempt/"verified_report.md"; target.write_text(verified,encoding="utf-8"); (run/"verified_report.md").write_text(verified,encoding="utf-8")
        print(f"Verified report: {target.resolve()}")
    except Exception as exc:
        (attempt/"FAILED.txt").write_text(str(exc),encoding="utf-8"); raise
    return verified

def _configure() -> None:
    load_dotenv(ROOT/".env"); key=os.getenv("API_KEY","").strip()
    if not key: raise ValueError("API_KEY is empty. Set it in .env.")
    os.environ["OPENAI_API_KEY"]=key

def run_team(ticker: str, peers: list[str], price: float | None) -> Path:
    _configure(); run,data=_steps(ticker,peers,price); evidence=_evidence(data); outputs={}
    try:
        for role in ROLE_ORDER: outputs[role]=_run_model(run,role,_packet(role,evidence,outputs),1)
        manager=_apply_insight_checks(_run_model(run,"Strategy Manager",_manager_packet(evidence,outputs),1),evidence,outputs); repairs=_repairs(manager)
        for role in ROLE_ORDER:
            if role in repairs: outputs[role]=_run_model(run,role,_packet(role,evidence,outputs,repairs[role]),2)
        if repairs: manager=_apply_insight_checks(_run_model(run,"Strategy Manager",_manager_packet(evidence,outputs),2),evidence,outputs)
        _json(run/"quality_checks.json", {"checks": manager.get("checks", []), "remaining_failures": [check for check in manager.get("checks", []) if isinstance(check, dict) and str(check.get("status")).lower() == "fail"]})
        report=_report(manager,data["filings"],outputs); (run/"report.md").write_text(report,encoding="utf-8")
        _verify(run,report,json.loads((run/"facts.json").read_text(encoding="utf-8")),data["texts"]); return run
    except Exception as exc:
        (run/"FAILED.txt").write_text(str(exc),encoding="utf-8"); raise

def verify_only(run: Path) -> Path:
    _configure(); report=(run/"report.md").read_text(encoding="utf-8"); saved=json.loads((run/"facts.json").read_text(encoding="utf-8")); texts={"10-K":(run/"10-K.txt").read_text(encoding="utf-8"),"10-Q":(run/"10-Q.txt").read_text(encoding="utf-8"),"8-K":(run/"8-K_Exhibit_99.1.txt").read_text(encoding="utf-8")}; _verify(run,report,saved,texts); return run

def main() -> None:
    p=argparse.ArgumentParser(); p.add_argument("ticker",nargs="?"); p.add_argument("--peers",nargs="*",default=[]); p.add_argument("--price",type=float); p.add_argument("--verify-only",type=Path); a=p.parse_args()
    if bool(a.ticker)==bool(a.verify_only): p.error("provide ticker or --verify-only, not both")
    peers=[x.strip().upper() for v in a.peers for x in v.split(",") if x.strip()]
    try: print(f"Saved run: {(verify_only(a.verify_only) if a.verify_only else run_team(a.ticker,peers,a.price)).resolve()}")
    except Exception as exc: print(f"Error: {exc}",file=sys.stderr); raise SystemExit(1)
if __name__=="__main__": main()
