# Scholarship Intelligence Crawler

A production-quality web crawler that discovers, extracts, verifies, and tracks scholarship opportunities for Indian students. It uses a **rule-based extraction pipeline** (regex + dateparser) with an optional Ollama LLM fallback, a **100-point weighted scoring system**, and a **Streamlit dashboard** for interactive exploration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SCHOLARSHIP INTELLIGENCE CRAWLER                │
└─────────────────────────────────────────────────────────────────────┘

   SEEDS + SEARCH QUERIES
          │
          ▼
   ┌─────────────┐    link_score()
   │  discover   │◄── keyword matching
   │  (discover) │    DuckDuckGo queries
   └──────┬──────┘
          │ candidate URLs
          ▼
   ┌─────────────┐    robots.txt check
   │   fetch     │    rate-limiting
   │  (fetch)    │    snapshot saving
   └──────┬──────┘    PDF extraction
          │ FetchResult(text, hash, snap_id)
          ▼
   ┌─────────────┐    is_scholarship_page()
   │  classify   │    tldextract domain check
   │ (classify)  │    gov/edu/foundation/CSR
   └──────┬──────┘
          │ (source_type, is_official)
          ▼
   ┌─────────────┐    DATE_RX / AMOUNT_RX / INCOME_RX
   │  extract    │◄── AGE_RX / PERCENT_RX
   │  (extract)  │    dateparser normalization
   └──────┬──────┘    Optional: Ollama LLM
          │ {field: (value, quote)}
          ▼
   ┌─────────────┐    100-point weighted scoring:
   │   verify    │    official_source (20)
   │  (verify)   │    present_on_source (15)
   └──────┬──────┘    application_url (10)
          │ (score, label, breakdown)   eligibility (15)
          ▼           deadline (10)
   ┌─────────────┐    freshness (5)
   │   store     │    no_conflict (10)
   │  (store)    │    consistency (10)
   └──────┬──────┘    traceability (5)
          │
          ▼
   ┌─────────────┐    diff_fields()
   │   changes   │    fingerprint matching
   │  (changes)  │    lifecycle rules
   └──────┬──────┘
          │
          ▼
   ┌─────────────┐
   │   SQLite DB │◄── data/scholarships.db
   └──────┬──────┘
          │
          ▼
   ┌─────────────┐
   │  Streamlit  │    Dashboard with filters,
   │  Dashboard  │    detail view, change history
   └─────────────┘
```

---

## Setup Instructions

### 1. Prerequisites

- Python 3.11+
- (Optional) [Ollama](https://ollama.com) for LLM extraction

### 2. Create Virtual Environment

```bash
cd c:\Users\DELL\OneDrive\Desktop\Scholarship_Crawler
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/Mac
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. (Optional) Enable LLM Support

Install Ollama and pull the model:
```bash
ollama pull llama3.1:8b
```

Then set `llm.enabled: true` in `config.yaml`.

---

## Running the Crawler

### Basic Run (rule-based only)
```bash
python -m src.run_crawl
```

### With flags
```bash
# Disable LLM explicitly
python -m src.run_crawl --no-llm

# Limit pages crawled
python -m src.run_crawl --max-pages 50

# Replay saved snapshots (for change-detection testing)
python -m src.run_crawl --replay
```

---

## Opening the Dashboard

```bash
streamlit run app/dashboard.py
```

Then open [http://localhost:8501](http://localhost:8501) in your browser.

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Scoring Methodology

| Dimension | Max Points | Description |
|-----------|-----------|-------------|
| Official Source | 20 | Gov/Edu domain = 20, Allowlisted foundation = 16, Unknown = 0 |
| Present on Source | 15 | Scholarship name found in fetched page text |
| Application URL | 10 | Valid application URL found and reachable |
| Eligibility | 15 | At least 2 eligibility fields populated |
| Deadline | 10 | Closing date present and in the future |
| Freshness | 5 | ≤7 days = 5, ≤30 days = 3, older = 0 |
| No Conflict | 10 | Rule-based and LLM results agree (or LLM disabled) |
| Consistency | 10 | Core fields populated and grounded |
| Traceability | 5 | All stored values have evidence quotes |

**Total: 100 points**

### Verification Labels

| Label | Threshold |
|-------|-----------|
| `VERIFIED` | ≥ 95 points AND official source |
| `REVIEW REQUIRED` | < 95 points OR non-official source |

---

## Minimum Targets

| Metric | Target |
|--------|--------|
| Scholarships discovered per run | ≥ 20 |
| Verified (≥95 score) | ≥ 10% |
| Change detection accuracy | 100% (no missed diffs) |
| False positives (non-scholarships) | < 5% |
| Test suite pass rate | 100% |

---

## Folder Structure

```
Scholarship_Crawler/
├── README.md                   # This file
├── requirements.txt            # Python dependencies
├── config.yaml                 # All configuration
├── .gitignore
├── .github/
│   └── workflows/
│       └── crawl.yml           # Daily GitHub Actions crawl
├── db/
│   └── schema.sql              # SQLite schema
├── src/
│   ├── __init__.py
│   ├── discover.py             # URL discovery pipeline
│   ├── classify.py             # Source type classification
│   ├── fetch.py                # HTTP fetching + snapshots
│   ├── extract.py              # Field extraction (regex + LLM)
│   ├── verify.py               # 100-point scoring engine
│   ├── changes.py              # Change detection
│   ├── store.py                # SQLite CRUD helpers
│   └── run_crawl.py            # Main orchestrator
├── app/
│   └── dashboard.py            # Streamlit dashboard
├── data/
│   └── snapshots/              # Saved page text snapshots
│       └── .gitkeep
└── tests/
    ├── __init__.py
    ├── test_grounding.py
    ├── test_extraction.py
    ├── test_scoring.py
    └── test_changes.py
```

---

## Notes

- The crawler **respects robots.txt** and applies per-domain rate limiting (default 2.5s).
- All extracted values are **grounded**: only stored if the supporting quote exists verbatim in the fetched page.
- The LLM (Ollama) is **entirely optional** — set `llm.enabled: false` in config for rule-only mode.
- Change detection uses **SHA-1 fingerprints** on `(name, provider, official_url)` for identity.
- The GitHub Actions workflow runs daily at 2:30 AM UTC and commits the updated database.
