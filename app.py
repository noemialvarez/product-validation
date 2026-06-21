"""
InsightSphere — Product Validation Agent
Streamlit app for B2B product evaluation using Claude AI + web search
"""

import streamlit as st
import anthropic
import re
import os
import json
import io
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import pypdf
import markdown as md_lib
from xhtml2pdf import pisa

REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


def _notion_enabled() -> bool:
    return bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID"))


def _notion_client():
    from notion_client import Client
    return Client(auth=os.environ["NOTION_TOKEN"])


def _chunk(s: str, n: int = 1900):
    """Split string into n-char chunks (Notion's limit per rich-text element is 2000)."""
    s = s or ""
    if not s:
        return [""]
    return [s[i : i + n] for i in range(0, len(s), n)]


# Notion DB schema (must match what the user creates):
#   "Product"      — title
#   "Date"         — date
#   "Web Search"   — checkbox
#   "Description"  — rich_text
_NOTION_SECTIONS = [
    ("synthesis",   "Strategic Synthesis"),
    ("market",      "Market Research"),
    ("competitors", "Competitor Intelligence"),
    ("customers",   "Customer Insights"),
]


def save_report_to_notion(product_name, product_desc, results, web_search: bool):
    client = _notion_client()
    now = datetime.now()
    payload = {
        "product_name": product_name,
        "product_desc": product_desc,
        "timestamp": now.strftime("%d %b %Y, %H:%M"),
        "web_search": web_search,
        "results": results,
    }
    json_blob = json.dumps(payload, ensure_ascii=False)

    properties = {
        "Product": {"title": [{"text": {"content": (product_name or "Untitled")[:200]}}]},
        "Date": {"date": {"start": now.isoformat()}},
        "Web Search": {"checkbox": bool(web_search)},
        "Description": {
            "rich_text": [{"text": {"content": (product_desc or "")[:1900]}}]
            if product_desc else []
        },
    }

    children = []
    # Human-readable sections — heading + paragraphs of the markdown body
    for key, label in _NOTION_SECTIONS:
        text = results.get(key, "") or ""
        if not text:
            continue
        children.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"text": {"content": label}}]},
        })
        for chunk in _chunk(text):
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
            })
    # Round-trip payload — full JSON in code blocks at the end of the page
    children.append({
        "object": "block",
        "type": "heading_3",
        "heading_3": {"rich_text": [{"text": {"content": "App data (do not edit)"}}]},
    })
    for chunk in _chunk(json_blob):
        children.append({
            "object": "block",
            "type": "code",
            "code": {
                "rich_text": [{"text": {"content": chunk}}],
                "language": "json",
            },
        })

    client.pages.create(
        parent={"database_id": os.environ["NOTION_DATABASE_ID"]},
        properties=properties,
        children=children,
    )


def load_reports_from_notion():
    client = _notion_client()
    # notion-client 3.x renamed databases.query → data_sources.query and now needs
    # the data source ID (not the database ID). Fetch the database to discover it.
    db = client.databases.retrieve(database_id=os.environ["NOTION_DATABASE_ID"])
    data_sources = db.get("data_sources") or []
    if not data_sources:
        return []
    ds_id = data_sources[0]["id"]
    results = client.data_sources.query(
        data_source_id=ds_id,
        sorts=[{"property": "Date", "direction": "descending"}],
        page_size=100,
    )
    reports = []
    for page in results.get("results", []):
        try:
            blocks = client.blocks.children.list(block_id=page["id"], page_size=200)
            # Concatenate every JSON code block back into the raw payload
            chunks = []
            for b in blocks.get("results", []):
                if b.get("type") == "code" and b["code"].get("language") == "json":
                    for rt in b["code"].get("rich_text", []):
                        chunks.append(rt.get("plain_text", ""))
            if not chunks:
                continue
            data = json.loads("".join(chunks))
            data["_file"] = page["id"]
            reports.append(data)
        except Exception:
            continue
    return reports


def save_report(product_name, product_desc, results, web_search: bool = False):
    """Save to Notion if configured; otherwise to the local filesystem."""
    if _notion_enabled():
        try:
            save_report_to_notion(product_name, product_desc, results, web_search)
            return
        except Exception as e:
            st.warning(f"Notion save failed: {e}. Falling back to local file.")
    slug = re.sub(r"[^a-z0-9]+", "-", product_name.lower()).strip("-")[:40]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"{ts}_{slug}.json"
    data = {
        "product_name": product_name,
        "product_desc": product_desc,
        "timestamp": datetime.now().strftime("%d %b %Y, %H:%M"),
        "web_search": web_search,
        "results": results,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _is_failed_report(data: dict) -> bool:
    """A report is failed if every section result starts with an error marker."""
    results = data.get("results", {})
    if not results:
        return True
    error_like = 0
    for v in results.values():
        if not isinstance(v, str):
            continue
        head = v.lstrip()[:200].lower()
        if head.startswith("**") and "error" in head:
            error_like += 1
    return error_like >= len(results)


def load_reports():
    """Load from Notion if configured; otherwise from the local filesystem."""
    if _notion_enabled():
        try:
            reports = load_reports_from_notion()
            return [r for r in reports if not _is_failed_report(r)]
        except Exception as e:
            st.warning(f"Notion load failed: {e}. Showing local files instead.")
    reports = []
    for f in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            if _is_failed_report(data):
                continue
            data["_file"] = f.name
            reports.append(data)
        except Exception:
            pass
    return reports

# ── InsightSphere CI Palette ─────────────────────────────────────────────────
PRIMARY   = "#00848C"   # Teal  — buttons, active tabs, header, key borders
SECONDARY = "#7048C1"   # Purple — separators, h3
ACCENT    = "#4564DA"   # Blue  — text, inactive tabs, light backgrounds
HIGHLIGHT = "#D6396B"   # Fuchsia — bold keywords, accents

# ── InsightSphere company context ─────────────────────────────────────────────
_CTX = """
INSIGHTSPHERE COMPANY CONTEXT (always personalise findings to this):
- Company: InsightSphere (insightsphere.co) — B2B SaaS startup, 3 years old, 2 years intensive
- Founder background: marketing; strong martech alignment
- Product journey: VR presentation training → web presentation skills → AI sales roleplay → conversational lead qualification
- Current product: real-time conversational AI lead qualification; interprets revenue signals from website visitor conversations
- ICP: B2B SaaS with complex products (50–500 employees); also exploring complex industrial products
- ICP pain: too many irrelevant inbound leads frustrating sales teams; pipeline quality > volume
- GTM: network-based acquisition, primarily Switzerland; first paying clients are the priority
- Also offers: conversational AI for internal company training (same core technology)
- Core competency: conversational AI; strong at retention, acquisition is current challenge
- Team: lean — one part-time developer, founder-led
"""

DEFAULT_PRODUCT = ""
DEFAULT_DESC = ""

# ── CSS ───────────────────────────────────────────────────────────────────────
# Design language ported from the Lovable/TanStack mockup:
#   - Warm stone background (#fafaf9)
#   - White cards with teal-300 border + top gradient stripe (pink → purple → cyan)
#   - Gradient text on display headings
#   - Rounded-full pill buttons with gradient fill
#   - Pill-shaped tabs with white-active state + soft shadow
CSS = """
<style>
/* ── Global ── */
.stApp { background: #fafaf9; }
section[data-testid="stSidebar"] { display: none; }
.block-container { padding-top: 2rem; max-width: 1200px; }

/* ── Header card — full gradient border around the card ── */
.is-header {
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
    padding: 4px;                              /* thicker still so top edge is unmissable */
    border-radius: 20px;
    margin: 24px 0 1.5rem;                     /* generous top margin keeps gradient from being clipped */
    box-shadow: 0 1px 3px 0 rgba(0,0,0,0.05);
}
.is-header-inner {
    background: white;
    border-radius: 16px;                       /* outer 20 − padding 4 */
    padding: 2.5rem 2.5rem 2rem;               /* extra padding-top pushes the logo down */
}
.is-header .logo-wrap img {
    height: 40px;
    display: block;
}
.is-header h1 {
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    color: transparent;
    margin: .85rem 0 1rem;
    font-size: 3rem;
    font-weight: 700;
    line-height: 1.05;
}
.is-header p { color: #6b7280; margin: 0; font-size: .95rem; }
.is-header p .chip { color: #0f172a; font-weight: 500; }

/* ── Inputs (text input + textarea) — borderless on light teal wash ── */
.stTextInput input,
.stTextArea textarea,
[data-baseweb="textarea"] textarea,
[data-baseweb="input"] input {
    border: 1px solid transparent !important;
    background: rgba(204, 251, 241, 0.30) !important;  /* teal-50/30 */
    border-radius: 8px !important;
    color: #0f172a;
}
/* Streamlit wraps inputs in extra div; strip its border too */
.stTextInput > div > div,
.stTextArea > div > div,
[data-baseweb="textarea"],
[data-baseweb="input"] {
    border-color: transparent !important;
    background: transparent !important;
}
.stTextInput input:focus,
.stTextArea textarea:focus {
    border-color: #2dd4bf !important;            /* teal-400 */
    box-shadow: 0 0 0 3px rgba(45, 212, 191, 0.25) !important;
    outline: none !important;
}
label, .stTextInput label, .stTextArea label {
    color: #0f172a !important;
    font-weight: 500 !important;
    font-size: .87rem !important;
}

/* ── Radio: render as pill chip group ── */
div[role="radiogroup"] {
    background: white;
    border: 1px solid rgba(94, 234, 212, 0.7);
    border-radius: 9999px;
    padding: 4px;
    display: inline-flex !important;
    gap: 2px;
}
div[role="radiogroup"] > label {
    border-radius: 9999px !important;
    padding: .35rem .9rem !important;
    margin: 0 !important;
    color: #6b7280 !important;
    font-size: .85rem !important;
    cursor: pointer;
    transition: all .15s ease;
}
div[role="radiogroup"] > label:has(input:checked) {
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
    color: white !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
/* Force the active pill's text (Streamlit nests <p>) to white */
div[role="radiogroup"] > label:has(input:checked),
div[role="radiogroup"] > label:has(input:checked) *,
div[role="radiogroup"] > label:has(input:checked) p {
    color: white !important;
}
div[role="radiogroup"] > label > div:first-child { display: none !important; } /* hide radio dot */

/* ── Run button — gradient pill ── */
div.stButton > button {
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
    color: white;
    border: none;
    border-radius: 9999px;
    padding: .75rem 1.75rem;
    font-weight: 600;
    font-size: .9rem;
    width: auto;
    box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.1);
    transition: opacity .15s ease, transform .15s ease;
}
div.stButton > button:hover {
    opacity: .92;
    transform: translateY(-1px);
}

/* ── Download button — outlined pill ── */
div.stDownloadButton > button {
    background: white;
    color: #0f766e;                              /* teal-700 */
    border: 1px solid rgba(94, 234, 212, 0.9);
    border-radius: 9999px;
    padding: .65rem 1.4rem;
    font-weight: 600;
    font-size: .85rem;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}
div.stDownloadButton > button:hover {
    background: rgba(204, 251, 241, 0.5);        /* teal-50/50 */
    color: #0f766e;
}

/* ── Toggle — bright cyan when ON, broad selectors to override Streamlit's red default ── */
[data-baseweb="checkbox"][aria-checked="true"] > div:first-child,
[data-baseweb="checkbox"][aria-checked="true"] > span,
[data-testid="stToggle"] [aria-checked="true"] > div:first-child,
[data-testid="stCheckbox"] [aria-checked="true"] > div:first-child,
[role="checkbox"][aria-checked="true"],
[role="switch"][aria-checked="true"],
.stCheckbox label > div:first-child > div:first-child[data-checked="true"],
.stCheckbox label[data-checked="true"] > div:first-child > div:first-child {
    background-color: #22d3ee !important;        /* cyan-400 */
    background: #22d3ee !important;
    border-color: #22d3ee !important;
}

/* ── Tabs — rounded-full pill list ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    background: white;
    border: 1px solid rgba(94, 234, 212, 0.7);
    border-radius: 9999px;
    padding: 4px;
    border-bottom: 1px solid rgba(94, 234, 212, 0.7);
}
.stTabs [data-baseweb="tab"] {
    background: transparent;
    border-radius: 9999px;
    border: none;
    color: #6b7280;
    font-weight: 500;
    padding: .5rem 1.1rem;
    font-size: .87rem;
    transition: all .15s ease;
}
.stTabs [data-baseweb="tab"]:hover { color: #0f172a; }
.stTabs [aria-selected="true"] {
    background: white !important;
    color: #0f172a !important;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.stTabs [data-baseweb="tab-highlight"],
.stTabs [data-baseweb="tab-border"] { display: none !important; }
.stTabs [data-baseweb="tab-panel"] {
    background: transparent;
    border: none;
    padding: 1.5rem 0 0;
}

/* ── Summary highlights — white card with stripe, colored bullets ── */
.summary-box {
    position: relative;
    overflow: hidden;
    background: white;
    border: 1px solid rgba(94, 234, 212, 0.7);
    border-radius: 16px;
    padding: 1.5rem 1.75rem;
    margin: .25rem 0 1.5rem;
    box-shadow: 0 1px 3px 0 rgba(0,0,0,0.05);
}
.summary-box::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 4px;
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
}
.summary-label {
    font-size: .72rem;
    text-transform: uppercase;
    letter-spacing: 2px;
    color: #0d9488;                              /* teal-600 */
    font-weight: 700;
    margin-bottom: 1rem;
}
.summary-box ul {
    list-style: none !important;
    padding: 0 !important;
    margin: 0 !important;
}
.summary-box li {
    display: flex;
    align-items: flex-start;
    gap: .75rem;
    margin: .65rem 0 !important;
    font-size: .92rem !important;
    line-height: 1.55 !important;
    color: #334155;
}
.summary-box li .dot {
    flex-shrink: 0;
    width: 6px; height: 6px;
    border-radius: 9999px;
    margin-top: .55rem;
}

/* ── Headings within content (markdown ## / ###) ── */
.stTabs [data-baseweb="tab-panel"] h2 {
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    color: transparent;
    font-size: 1.35rem;
    font-weight: 600;
    border: none;
    padding: 0;
    margin: 1.4rem 0 .6rem;
}
.stTabs [data-baseweb="tab-panel"] h3 {
    color: #a855f7;
    font-weight: 600;
    font-size: 1.05rem;
    margin: 1rem 0 .4rem;
}
.stTabs [data-baseweb="tab-panel"] p,
.stTabs [data-baseweb="tab-panel"] li { color: #334155; }
.stTabs [data-baseweb="tab-panel"] strong { color: #db2777; }   /* pink-600 */
/* hr lines — teal everywhere (force, override Streamlit defaults) */
hr,
.stTabs [data-baseweb="tab-panel"] hr,
[data-testid="stMarkdownContainer"] hr,
[data-testid="stHorizontalBlock"] hr {
    border: none !important;
    border-top: 1px solid #5eead4 !important;     /* teal-300 */
    background: transparent !important;
    color: transparent !important;
    height: 0 !important;
    margin: 1.2rem 0 !important;
}

/* ── Tables — turquoise header, teal borders ── */
.stTabs [data-baseweb="tab-panel"] table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    margin: 1rem 0;
    font-size: .87rem;
    border: 1px solid #5eead4;                    /* teal-300 */
    border-radius: 12px;
    overflow: hidden;
}
.stTabs [data-baseweb="tab-panel"] th {
    background: #14b8a6 !important;               /* teal-500 turquoise */
    color: white !important;
    padding: .7rem 1rem;
    text-align: left;
    font-weight: 600;
    border: none;
}
.stTabs [data-baseweb="tab-panel"] td {
    padding: .65rem 1rem;
    border-top: 1px solid rgba(204, 251, 241, 0.7);
    color: #475569;
}
.stTabs [data-baseweb="tab-panel"] tr:nth-child(even) td {
    background: rgba(240, 253, 250, 0.6);        /* teal-50/60 */
}

/* ── File uploader (PDF upload mode) ── */
[data-testid="stFileUploaderDropzone"] {
    border: 2px dashed #5eead4 !important;       /* teal-300 */
    background: rgba(204, 251, 241, 0.25) !important;
    border-radius: 12px !important;
}

/* ── Status info/success/error banners ── */
[data-testid="stAlertContainer"] { border-radius: 12px; }

/* ── Selectbox (in Previous Reports) ── */
[data-baseweb="select"] > div {
    border: 1px solid #99f6e4 !important;
    background: rgba(204, 251, 241, 0.25) !important;
    border-radius: 8px !important;
}

/* Dropdown chevron — replaced with a thick, gradient-tinted custom chevron */
[data-baseweb="select"] [aria-hidden="true"] svg { display: none !important; }
[data-baseweb="select"] [aria-hidden="true"]::after {
    content: '';
    display: inline-block;
    width: 26px;
    height: 26px;
    background: linear-gradient(90deg, #ec4899, #a855f7, #22d3ee);
    -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>") center/22px no-repeat;
            mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>") center/22px no-repeat;
}
</style>
"""

# ── Prompts ───────────────────────────────────────────────────────────────────
def _market_prompt(name, desc):
    return f"""You are a senior B2B market research analyst. Research the market for this product.

PRODUCT: {name}
DESCRIPTION: {desc}
{_CTX}

CRITICAL: B2B ONLY. No B2C data. Use web search for 2024-2026 data from analyst reports and news.

Output structure (follow exactly):

## SUMMARY HIGHLIGHTS
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words

---

## Market Size & Growth
TAM/SAM estimate + CAGR. Flag estimated vs cited.

## Key Trends (2024-2026)
5 B2B-specific trends with evidence.

## Strongest Opportunity Segments
3 segments: ICP, company size, vertical, and why attractive now.

## Market Maturity
Early / Growth / Mature with reasoning specific to this product.

## Critical Risks
3 risks specific to this product entering this market now."""


def _competitor_prompt(name, desc):
    return f"""You are a competitive intelligence analyst mapping the B2B landscape.

PRODUCT: {name}
DESCRIPTION: {desc}
{_CTX}

CRITICAL: B2B only. Use web search for current pricing, positioning, and recent news.

Output structure (follow exactly):

## SUMMARY HIGHLIGHTS
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words

---

## Competitor Map

| Competitor | Positioning | Target ICP | Pricing Signal | Key Weakness |
|---|---|---|---|---|

Include 6-8 real competitors: dedicated tools, CRM-embedded AI, AI-native startups (2023-2025), adjacent players expanding into this space.

## Positioning Gaps & White Space
3-4 paragraphs on underserved segments and open positioning territory.

## What Would Make This Defensible
Moat strategies given InsightSphere's specific strengths (conversational AI, retention, lean team).

## Immediate Competitive Threats
Top 3 threats a new entrant faces in this space right now."""


def _customer_prompt(name, desc):
    return f"""You are a B2B customer research analyst. Find what real B2B buyers say about AI lead qualification tools.

PRODUCT: {name}
DESCRIPTION: {desc}
{_CTX}

CRITICAL: B2B ONLY. Personas: sales ops, revenue ops, marketing ops, SDR managers, VP Sales.
Use web search: G2, Capterra, TrustRadius, Reddit (r/sales, r/salesops, r/b2bmarketing), LinkedIn.

Output structure (follow exactly):

## SUMMARY HIGHLIGHTS
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words

---

## Top Pain Points with Current Tools
6-7 concrete pain points. Include paraphrased user quotes where possible.

## Unmet Needs & Feature Gaps
4-5 things buyers say don't exist or aren't done well anywhere in the market.

## Table Stakes
Must-have features to even be considered — what every serious competitor already has.

## Who Actually Buys
Specific persona: title, company size, budget authority, evaluation criteria and process.

## Switching Triggers
Specific events or conditions that cause buyers to adopt new tools in this category."""


def _synthesis_prompt(name, desc, market, competitors, customers):
    return f"""You are a senior B2B product strategist. Synthesise all research into a founder recommendation.

PRODUCT: {name}
DESCRIPTION: {desc}
{_CTX}

MARKET RESEARCH FINDINGS:
{market[:2500]}

COMPETITOR INTELLIGENCE:
{competitors[:2500]}

CUSTOMER INSIGHTS:
{customers[:2500]}

Output structure (follow exactly):

## SUMMARY HIGHLIGHTS
- **[Keyword]:** the single most important insight for this founder (max 15 words)
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words
- **[Keyword]:** crisp insight in max 15 words

---

## Opportunity Scorecard

| Dimension | Score | Rationale |
|---|---|---|
| Problem Severity | X/5 | one line |
| Market Timing | X/5 | one line |
| Competitive Intensity (lower = better) | X/5 | one line |
| Willingness to Pay | X/5 | one line |
| Differentiation Potential | X/5 | one line |
| **Overall** | **X/5** | one line |

## For InsightSphere Specifically
2-3 paragraphs connecting findings to InsightSphere's specific situation: conversational AI competency, retention strength, lean team, Switzerland-first GTM, and first-client priority.

## Top 3 Risks + Mitigations
For each risk: what it is and a specific mitigation strategy.

## Differentiation Bets Worth Testing
3-4 concrete strategic bets, ordered by feasibility for a lean founder-led team.

## Verdict

BUILD / PIVOT / DISCARD — [plain-English paragraph]

BUILD IF: [specific conditions that must be true]
PIVOT IF: [specific conditions that would trigger a pivot]
DISCARD IF: [specific conditions that would kill the idea]

## Immediate Next 3 Actions
Specific steps to take this week to test the most critical assumption."""


# ── API call ──────────────────────────────────────────────────────────────────
def call_claude(prompt: str, use_web_search: bool = False) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return (
            "**ANTHROPIC_API_KEY not set.**\n\n"
            "Add it to your environment:\n"
            "```\nexport ANTHROPIC_API_KEY=sk-ant-...\n```\n"
            "Then restart the app."
        )

    client = anthropic.Anthropic(api_key=api_key)
    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    if use_web_search:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]

    response = client.messages.create(**kwargs)
    return "".join(
        block.text for block in response.content if hasattr(block, "text")
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_sections(text: str):
    """Split response into (summary_md, body_md)."""
    m = re.search(r"## SUMMARY HIGHLIGHTS\n(.*?)(?=\n---\n|\Z)", text, re.DOTALL)
    if not m:
        return None, text
    summary = m.group(1).strip()
    rest = text[m.end():].lstrip()
    if rest.startswith("---"):
        rest = rest[3:].lstrip()
    return summary, rest


def summary_html(md: str) -> str:
    """Convert bullet markdown to a styled highlight box with cycling brand colors."""
    # Cycle through brand accent colors per item, matching the Lovable mockup.
    palette = [
        ("#f43f5e", "#e11d48"),   # rose-500 dot, rose-600 label
        ("#a855f7", "#9333ea"),   # purple-500 / purple-600
        ("#f59e0b", "#d97706"),   # amber-500 / amber-600
        ("#14b8a6", "#0d9488"),   # teal-500 / teal-600
        ("#ec4899", "#db2777"),   # pink-500 / pink-600
    ]
    items = []
    idx = 0
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:]
        dot, label_col = palette[idx % len(palette)]
        idx += 1
        # Recolor the **bold** lead-in to the item's accent color and drop the asterisks.
        body = re.sub(
            r"\*\*(.*?)\*\*",
            lambda m: f"<span style='color:{label_col};font-weight:600'>{m.group(1)}</span>",
            body,
        )
        items.append(
            f"<li>"
            f"<span class='dot' style='background:{dot}'></span>"
            f"<span>{body}</span>"
            f"</li>"
        )
    bullets = "".join(items)
    return (
        "<div class='summary-box'>"
        "<div class='summary-label'>Key Highlights</div>"
        f"<ul>{bullets}</ul>"
        "</div>"
    )


# ── PDF export ────────────────────────────────────────────────────────────────
# Strategic Synthesis first — it's the founder-facing recommendation.
PDF_SECTIONS = [
    ("synthesis",   "Strategic Synthesis"),
    ("market",      "Market Research"),
    ("competitors", "Competitor Intelligence"),
    ("customers",   "Customer Insights"),
]

PDF_CSS = """
@page {
    size: A4;
    margin: 1.8cm 1.5cm 2.2cm 1.5cm;          /* extra bottom margin for the footer */
    background-color: #fafaf9;
    @frame footer_frame {
        -pdf-frame-content: footer_content;
        left: 1.5cm;
        right: 1.5cm;
        bottom: 1cm;
        height: 0.8cm;
    }
}
.footer {
    text-align: center;
    font-size: 8pt;
    color: #6b7280;
    border-top: 1px solid #5eead4;
    padding-top: .3em;
}
body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #334155; line-height: 1.5; }

/* Cards: white surface, NO teal borders (gradient stripe alone signals the brand) */
.card {
    background: white;
    padding: 1em 1.2em;
    margin: .6em 0 1.2em;
}
.stripe { width: 100%; border-collapse: collapse; margin: 0 0 .9em; }
.stripe td { padding: 0; height: 4px; border: none; }

/* Cover title — "Idea Validation" in the gradient's purple stop (xhtml2pdf can't do gradient text) */
h1.cover-title {
    font-size: 30pt;
    color: #a855f7;
    margin: 0 0 .25em;
    font-weight: bold;
    line-height: 1.05;
}
.cover-subtitle {
    color: #6b7280;
    font-size: 10pt;
    margin: 0 0 .5em;
}
.cover-subtitle span { color: #0f172a; font-weight: 500; }
.cover-meta { color: #6b7280; font-size: 9pt; margin: .4em 0 0; }

/* Input-style fields on the cover, mirroring the app's input panel */
.field-label {
    font-size: 7.5pt;
    color: #475569;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    font-weight: bold;
    margin: .8em 0 .25em;
}
.field-value {
    font-size: 10pt;
    color: #0f172a;
    background: rgba(204, 251, 241, 0.30);   /* same light teal wash as app inputs */
    padding: .6em .8em;
    border-radius: 6px;
    margin: 0;
}
.cover-desc { color: #475569; font-size: 9.5pt; }

h1.section-title {
    font-size: 17pt;
    color: #a855f7;                       /* purple-500 */
    border-bottom: 1px solid #e5e7eb;     /* light grey, no more teal */
    padding-bottom: .3em;
    margin: 0 0 .8em;
    font-weight: bold;
}

h2 { font-size: 12.5pt; color: #db2777; margin: 1.1em 0 .35em; font-weight: bold; }
h3 { font-size: 11pt; color: #a855f7; margin: .9em 0 .3em; font-weight: bold; }

p { margin: .35em 0; }
strong { color: #db2777; }                /* pink-600 */

/* Summary highlights card */
.summary-label {
    font-size: 7.5pt;
    color: #0d9488;                       /* teal-600 */
    letter-spacing: 2px;
    font-weight: bold;
    margin-bottom: .5em;
    text-transform: uppercase;
}
.summary-list { margin: 0; padding: 0; }
.summary-item { margin: .4em 0; font-size: 9.5pt; line-height: 1.5; }

/* Tables in markdown bodies — turquoise header, no teal outer border */
table { width: 100%; border-collapse: collapse; margin: .8em 0; font-size: 9pt; }
th {
    background: #14b8a6;                  /* teal-500 turquoise (matches the app's tables) */
    color: white;
    padding: .5em .7em;
    text-align: left;
    font-weight: bold;
    border: none;
}
td { padding: .45em .7em; border-bottom: 1px solid #e5e7eb; vertical-align: top; color: #475569; }
tr:nth-child(even) td { background: #f0fdfa; }   /* teal-50 alternating row tint */

ul, ol { margin: .4em 0 .8em 1em; padding-left: 1em; }
li { margin: .25em 0; }
a { color: #0d9488; text-decoration: none; }

/* Table of contents — no teal border */
.toc {
    background: white;
    padding: 1em 1.3em;
    margin: 1em 0 1.4em;
}
.toc-title { font-size: 13pt; color: #a855f7; font-weight: bold; margin: 0 0 .7em; }
.toc-main { font-weight: bold; color: #0d9488; margin-top: .55em; font-size: 10.5pt; }
.toc-sub { margin-left: 1.3em; font-size: 9.5pt; color: #6b7280; margin-top: .15em; }

/* Horizontal rules — light grey, never black */
hr { border: none; border-top: 1px solid #e5e7eb; margin: .8em 0; }

.page-break { page-break-before: always; }
"""

# Three-cell colored mini-table that approximates the pink → purple → cyan
# gradient stripe of the UI cards. xhtml2pdf can't render CSS gradients, so
# we fake it with three solid-color table cells side by side.
_GRADIENT_STRIPE = (
    '<table class="stripe"><tr>'
    '<td style="background:#ec4899"></td>'
    '<td style="background:#a855f7"></td>'
    '<td style="background:#22d3ee"></td>'
    "</tr></table>"
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def _section_headings(body_md: str):
    """Return list of (heading_text, slug) for ## headings in a body."""
    out = []
    for line in body_md.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            text = m.group(1).strip()
            out.append((text, _slug(text)))
    return out


def _md_to_html_with_anchors(body_md: str, key_prefix: str) -> str:
    """Markdown → HTML, injecting anchor ids on each h2 (## heading)."""
    html = md_lib.markdown(body_md, extensions=["tables", "fenced_code"])

    def add_anchor(match):
        inner = match.group(1)
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        anchor = f"{key_prefix}-{_slug(plain)}"
        return f'<a name="{anchor}"></a><h2>{inner}</h2>'

    return re.sub(r"<h2>(.*?)</h2>", add_anchor, html, flags=re.DOTALL)


def _summary_html_pdf(summary_md: str) -> str:
    # Cycle the same brand colors used in the UI for visual parity.
    palette = [
        ("#f43f5e", "#e11d48"),   # rose
        ("#a855f7", "#9333ea"),   # purple
        ("#f59e0b", "#d97706"),   # amber
        ("#14b8a6", "#0d9488"),   # teal
        ("#ec4899", "#db2777"),   # pink
    ]
    rows = []
    idx = 0
    for line in summary_md.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:]
        dot, label_col = palette[idx % len(palette)]
        idx += 1
        body = re.sub(
            r"\*\*(.*?)\*\*",
            lambda m, col=label_col: f'<font color="{col}"><b>{m.group(1)}</b></font>',
            body,
        )
        # xhtml2pdf flexbox/inline-block support is weak — use a 2-cell table for the dot + text.
        rows.append(
            '<table style="width:100%;border-collapse:collapse;margin:.3em 0;">'
            "<tr>"
            f'<td style="width:14px;padding:0;border:none;vertical-align:top;">'
            f'  <div style="width:6px;height:6px;background:{dot};margin-top:.55em;"></div>'
            "</td>"
            f'<td style="padding:0 0 0 .3em;border:none;font-size:9.5pt;line-height:1.5;color:#334155;">{body}</td>'
            "</tr></table>"
        )
    return (
        '<div class="card">'
        + _GRADIENT_STRIPE
        + '<div class="summary-label">Key Highlights</div>'
        + "".join(rows)
        + "</div>"
    )


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_pdf_bytes(report: dict) -> bytes:
    """Build a styled PDF (with clickable TOC) for a report dict."""
    parts = []
    name = _escape_html(report.get("product_name", "Product"))
    desc = _escape_html(report.get("product_desc", ""))
    ts = report.get("timestamp", "")
    source = "Live web search" if report.get("web_search") else "Training knowledge"

    # Cover header — gradient title "Idea Validation" + four chip subtitle (mirrors the app header)
    parts.append(
        '<div class="card">'
        + _GRADIENT_STRIPE
        + '<h1 class="cover-title">Idea Validation</h1>'
        + '<div class="cover-subtitle">'
          '<span>B2B market intelligence</span> &middot; '
          '<span>Competitor mapping</span> &middot; '
          '<span>Customer insights</span> &middot; '
          '<span>Strategic synthesis</span>'
        '</div>'
        + "</div>"
    )

    # Input-style card showing what the user entered (mirrors the app input panel)
    parts.append(
        '<div class="card">'
        + _GRADIENT_STRIPE
        + '<div class="field-label">Product to evaluate</div>'
        + f'<div class="field-value">{name}</div>'
        + (
            '<div class="field-label">Brief description</div>'
            f'<div class="field-value">{desc}</div>'
            if desc else ""
        )
        + f'<div class="cover-meta">{_escape_html(ts)} &middot; {source}</div>'
        + "</div>"
    )

    # Table of contents
    toc = ['<div class="toc">', _GRADIENT_STRIPE, '<div class="toc-title">Contents</div>']
    for key, label in PDF_SECTIONS:
        text = report.get("results", {}).get(key, "")
        if not text:
            continue
        _, body = parse_sections(text)
        section_anchor = f"section-{key}"
        toc.append(
            f'<div class="toc-main"><a href="#{section_anchor}">{label}</a></div>'
        )
        for h_text, h_slug in _section_headings(body):
            anchor = f"{key}-{h_slug}"
            toc.append(
                f'<div class="toc-sub"><a href="#{anchor}">{_escape_html(h_text)}</a></div>'
            )
    toc.append("</div>")
    parts.append("".join(toc))

    # Each section on its own page
    for key, label in PDF_SECTIONS:
        text = report.get("results", {}).get(key, "")
        if not text:
            continue
        summ, body = parse_sections(text)
        section_anchor = f"section-{key}"
        parts.append('<div class="page-break"></div>')
        parts.append(_GRADIENT_STRIPE)
        parts.append(
            f'<a name="{section_anchor}"></a>'
            f'<h1 class="section-title">{label}</h1>'
        )
        if summ:
            parts.append(_summary_html_pdf(summ))
        parts.append(_md_to_html_with_anchors(body, key))

    # Footer with page numbers — xhtml2pdf repeats the #footer_content frame on every page.
    footer = (
        '<div id="footer_content" class="footer">'
        'Page <pdf:pagenumber/> of <pdf:pagecount/>'
        "</div>"
    )

    html = (
        "<html><head><meta charset='utf-8'>"
        f"<style>{PDF_CSS}</style></head><body>"
        f"{footer}"
        f"{''.join(parts)}</body></html>"
    )
    buf = io.BytesIO()
    pisa.CreatePDF(src=html, dest=buf, encoding="utf-8")
    return buf.getvalue()


def _pdf_filename(product_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", product_name.lower()).strip("-")[:40] or "report"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{slug}.pdf"


def render_tab(key: str):
    """Render summary box + body for a given session_state key."""
    text = st.session_state.get(key, "")
    if not text:
        st.info("Run the evaluation to see results here.")
        return
    summ, body = parse_sections(text)
    if summ:
        st.markdown(summary_html(summ), unsafe_allow_html=True)
    st.markdown(body)


def _render_reports():
    reports = load_reports()
    if not reports:
        st.info("No saved reports yet. Run an evaluation to save your first report.")
        return

    st.markdown(f"**{len(reports)} saved report{'s' if len(reports) != 1 else ''}**")
    options = {
        f"{'🌐' if r.get('web_search') else '📚'}  {r['timestamp']} — {r['product_name']}": r
        for r in reports
    }
    choice = st.selectbox("Select a report", list(options.keys()), label_visibility="collapsed")
    selected = options[choice]

    source_label = "🌐 Live web search" if selected.get("web_search") else "📚 Training knowledge"
    st.markdown(f"### {selected['product_name']}")
    st.caption(f"{selected['timestamp']} · {source_label}")
    st.markdown(f"> {selected['product_desc'][:200]}{'…' if len(selected['product_desc']) > 200 else ''}")

    try:
        pdf_bytes = build_pdf_bytes(selected)
        st.download_button(
            "📄  Download as PDF",
            data=pdf_bytes,
            file_name=_pdf_filename(selected["product_name"]),
            mime="application/pdf",
            key=f"dl_{selected.get('_file', selected['timestamp'])}",
        )
    except Exception as e:
        st.warning(f"PDF generation failed: {e}")

    st.markdown("---")

    r1, r2, r3, r4 = st.tabs([
        "🎯  Strategic Synthesis",
        "📊  Market Research",
        "🏆  Competitor Intelligence",
        "💬  Customer Insights",
    ])
    for tab, key in zip((r1, r2, r3, r4), ("synthesis", "market", "competitors", "customers")):
        with tab:
            text = selected["results"].get(key, "")
            summ, body = parse_sections(text)
            if summ:
                st.markdown(summary_html(summ), unsafe_allow_html=True)
            st.markdown(body)


# ── Main ──────────────────────────────────────────────────────────────────────
def _check_password() -> bool:
    """Shared-password gate. Reads APP_PASSWORD from env.
    If APP_PASSWORD is unset (e.g. local dev), the gate is disabled."""
    if st.session_state.get("authenticated"):
        return True

    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        return True  # local dev: no password set, let through

    # Compact branded login card
    st.markdown(
        """
        <div class="is-header">
            <div class="is-header-inner">
                <div class="logo-wrap">
                    <img src="https://insightsphere.co/wp-content/uploads/2024/03/logo-insightsphere.svg"
                         alt="InsightSphere">
                </div>
                <h1>Product Validation</h1>
                <p><span class="chip">Sign in to continue</span></p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col, _ = st.columns([1, 2])
    with col:
        with st.form("login_form", clear_on_submit=False):
            pw = st.text_input("Password", type="password", label_visibility="collapsed",
                               placeholder="Password")
            ok = st.form_submit_button("Sign in")
        if ok:
            if pw == expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
    return False


def main():
    st.set_page_config(
        page_title="InsightSphere · Product Validation",
        page_icon="🔍",
        layout="wide",
    )
    st.markdown(CSS, unsafe_allow_html=True)

    if not _check_password():
        return

    # ── Header (gradient border around the whole card) ──
    st.markdown(
        """
        <div class="is-header">
            <div class="is-header-inner">
                <div class="logo-wrap">
                    <img src="https://insightsphere.co/wp-content/uploads/2024/03/logo-insightsphere.svg"
                         alt="InsightSphere">
                </div>
                <h1>Product Validation</h1>
                <p>
                    <span class="chip">B2B market intelligence</span> &nbsp;·&nbsp;
                    <span class="chip">Competitor mapping</span> &nbsp;·&nbsp;
                    <span class="chip">Customer insights</span> &nbsp;·&nbsp;
                    <span class="chip">Strategic synthesis</span>
                </p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Input panel — source toggle on top left, then product name, then full-width description ──
    src_col, _ = st.columns([1, 2])
    with src_col:
        source_mode = st.radio(
            "Description source",
            ["✏️ Write description", "📄 Upload PDF"],
            horizontal=True,
            label_visibility="collapsed",
        )

    product_name = st.text_input(
        "Product to evaluate",
        value=st.session_state.get("product_name", DEFAULT_PRODUCT),
        placeholder="e.g. AI Lead Qualification Agent",
    )

    if source_mode == "✏️ Write description":
        product_desc = st.text_area(
            "Brief description",
            value=st.session_state.get("product_desc", DEFAULT_DESC),
            height=160,
            placeholder="Describe your product, target ICP, and core value proposition...",
        )
    else:
        uploaded_pdf = st.file_uploader("Upload a PDF", type="pdf", label_visibility="collapsed")
        if uploaded_pdf:
            reader = pypdf.PdfReader(io.BytesIO(uploaded_pdf.read()))
            pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            product_desc = pdf_text[:4000]  # cap to avoid oversized prompts
            st.success(f"✅ PDF loaded — {len(reader.pages)} page{'s' if len(reader.pages) != 1 else ''}, {len(product_desc)} characters extracted")
        else:
            product_desc = st.session_state.get("product_desc", DEFAULT_DESC)
            st.info("Upload a PDF to use it as the product description.")

    col_btn, col_toggle = st.columns([3, 1])
    with col_btn:
        run = st.button("🚀  Run Full Evaluation")
    with col_toggle:
        use_web_search = st.toggle(
            "🌐 Web search",
            value=st.session_state.get("web_search", True),
            help="Slower but pulls live data. Turn off for faster results using Claude's training knowledge.",
        )

    # ── Run evaluation (3 sections in parallel, synthesis after) ──
    if run:
        st.session_state.product_name = product_name
        st.session_state.product_desc = product_desc
        st.session_state.web_search = use_web_search
        for k in ("market", "competitors", "customers", "synthesis"):
            st.session_state.pop(k, None)

        results = {}

        # Status indicators — update in real time as each section finishes
        st.markdown("#### Running evaluation…")
        col1, col2, col3 = st.columns(3)
        s_market      = col1.empty()
        s_competitors = col2.empty()
        s_customers   = col3.empty()
        s_synthesis   = st.empty()

        s_market.info("📊 Market Research…")
        s_competitors.info("🏆 Competitor Intelligence…")
        s_customers.info("💬 Customer Insights…")
        s_synthesis.info("🎯 Synthesis — waiting for above…")

        _labels = {
            "market":      ("📊 Market Research",       s_market),
            "competitors": ("🏆 Competitor Intelligence", s_competitors),
            "customers":   ("💬 Customer Insights",      s_customers),
        }

        def _extract_summary(text):
            m = re.search(r"## SUMMARY HIGHLIGHTS\n(.*?)(?=\n---\n|\Z)", text, re.DOTALL)
            return m.group(1).strip() if m else text[:800]

        # Run first 3 sequentially so web search tokens don't overlap.
        # Store each result into session_state immediately so stale values never persist.
        for key, prompt, label_tuple in [
            ("market",      _market_prompt(product_name, product_desc),      ("📊 Market Research",        s_market)),
            ("competitors", _competitor_prompt(product_name, product_desc),  ("🏆 Competitor Intelligence", s_competitors)),
            ("customers",   _customer_prompt(product_name, product_desc),    ("💬 Customer Insights",       s_customers)),
        ]:
            label, placeholder = label_tuple
            placeholder.info(f"⏳ {label}…")
            try:
                results[key] = call_claude(prompt, use_web_search)
                st.session_state[key] = results[key]
                placeholder.success(f"✅ {label} done")
            except Exception as e:
                results[key] = f"**{label} error:** {e}\n\nTry clicking Run again in a few seconds."
                st.session_state[key] = results[key]
                placeholder.error(f"⚠️ {label} failed")

        s_synthesis.info("🎯 Synthesising findings…")
        try:
            results["synthesis"] = call_claude(
                _synthesis_prompt(
                    product_name, product_desc,
                    _extract_summary(results["market"]),
                    _extract_summary(results["competitors"]),
                    _extract_summary(results["customers"]),
                )
            )
            st.session_state["synthesis"] = results["synthesis"]
            s_synthesis.success("✅ Synthesis done")
        except Exception as e:
            results["synthesis"] = f"**Synthesis error:** {e}\n\nTry clicking Run again in a few seconds."
            st.session_state["synthesis"] = results["synthesis"]
            s_synthesis.error("⚠️ Synthesis failed — see Synthesis tab")


        save_report(product_name, product_desc, results, web_search=use_web_search)
        st.rerun()

    # ── Download as PDF (only when a full evaluation is in session_state) ──
    if all(st.session_state.get(k) for k in ("market", "competitors", "customers", "synthesis")):
        try:
            pdf_bytes = build_pdf_bytes({
                "product_name": st.session_state.get("product_name", product_name),
                "product_desc": st.session_state.get("product_desc", product_desc),
                "timestamp": datetime.now().strftime("%d %b %Y, %H:%M"),
                "web_search": st.session_state.get("web_search", False),
                "results": {k: st.session_state[k] for k in ("market", "competitors", "customers", "synthesis")},
            })
            st.download_button(
                "📄  Download as PDF",
                data=pdf_bytes,
                file_name=_pdf_filename(st.session_state.get("product_name", product_name)),
                mime="application/pdf",
            )
        except Exception as e:
            st.warning(f"PDF generation failed: {e}")

    # ── Display results in tabs (always shown) ──
    t1, t2, t3, t4, t5 = st.tabs([
        "🎯  Strategic Synthesis",
        "📊  Market Research",
        "🏆  Competitor Intelligence",
        "💬  Customer Insights",
        "📁  Previous Reports",
    ])
    with t1:
        render_tab("synthesis")
    with t2:
        render_tab("market")
    with t3:
        render_tab("competitors")
    with t4:
        render_tab("customers")
    with t5:
        _render_reports()


if __name__ == "__main__":
    main()
