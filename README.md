Emerson MCP Server — Test Bed
A parallel implementation of the Emerson investment intelligence system as an MCP (Model Context Protocol) server. This gives Claude direct tool access to the data layer, regime state machine, thesis lifecycle, and SEC research — without waiting for the full Akra Collective frontend build.

What This Is
The production Emerson system (Joseph at Akra) is a full web application with React frontend, PostgreSQL, and six views. This MCP server implements the analytical engine — the same five conceptual layers from the spec — as callable tools:

Layer	Production (Akra)	MCP Test Bed
Sensory	Polling scheduler, webhook receiver	emerson_fetch_indicators, emerson_fred_history
Classification	Backend state machine + DB	emerson_evaluate_regime (pure function)
Synthesis	Backend-proxied Claude API	Claude itself (you're already in it)
Organization	PostgreSQL + CRUD APIs	emerson_*_thesis tools (in-memory store)
Presentation	React frontend	Claude's responses, formatted from tool data
Quick Start
1. Install dependencies
bash cd emerson_mcp pip install -r requirements.txt

2. Set API keys
```bash export FRED_API_KEY="your_key_here" # Free: https://fred.stlouisfed.org/docs/api/api_key.html export ALPHA_VANTAGE_API_KEY="your_key" # Free: https://www.alphavantage.co/support/#api-key

EDGAR requires no API key — just a User-Agent header (auto-configured)
```

3. Run the server
With Claude Desktop — add to your claude_desktop_config.json:

json { "mcpServers": { "emerson": { "command": "python", "args": ["/path/to/emerson_mcp/server.py"], "env": { "FRED_API_KEY": "your_key", "ALPHA_VANTAGE_API_KEY": "your_key" } } } }

With Claude Code:

bash claude mcp add emerson python /path/to/emerson_mcp/server.py

Standalone test:

bash python server.py

4. Phone Access (Later)
When ready for mobile, change the last line of server.py:

```python

Change from:
mcp.run()

To:
mcp.run(transport="streamable_http", port=8000) ```

Then deploy to a server (Railway, Render, AWS) and connect from Claude.ai mobile via the MCP connector URL.

Tool Inventory
Morning Scrum
Tool	What It Does
emerson_morning_scrum	Primary entry point. Fetches all data, runs regime assessment, checks stale theses, shows upcoming catalysts + monitoring window, assembles daily briefing with stat cards.
Regime Monitor
Tool	What It Does
emerson_fetch_indicators	Pull current values from FRED + Alpha Vantage for all 13 monitored indicators
emerson_evaluate_regime	Run the five-state classifier on fetched data (risk_on → crisis) with anti-whipsaw
emerson_list_indicators	Show all indicator definitions, thresholds, and composites
Thesis Registry
Tool	What It Does
emerson_create_thesis	Create a new thesis (starts at Spark stage)
emerson_list_theses	List all theses, optionally filtered by stage
emerson_get_thesis	Full detail on a specific thesis
emerson_update_thesis	Update fields, add notes
emerson_transition_thesis	Move thesis through lifecycle (Spark → Draft → Active → Fortress → Closed)
EDGAR Research
Tool	What It Does
emerson_edgar_filings	Company filing history by CIK
emerson_edgar_facts	Structured XBRL financial data (balance sheet, income statement, cash flow)
emerson_edgar_search	Full-text search across all SEC filings
Catalyst Calendar
Tool	What It Does
emerson_calendar_upcoming	List upcoming catalyst events (FOMC, BoJ, CPI, earnings, etc.)
emerson_calendar_monitoring	Show events in active monitoring window (Weather layer engaged)
emerson_calendar_by_thesis	Find all catalysts linked to a specific thesis
emerson_calendar_add	Add a custom catalyst event
emerson_calendar_debrief	Add post-event debrief (what happened, delta, thesis impact)
Field Notes — The Ledger
Tool	What It Does
emerson_note_create	Log a chart observation, reading spark, or insight
emerson_note_list	List recent field notes
emerson_note_search	Search notes by keyword
emerson_note_by_ticker	Find all notes tagged with a ticker
emerson_note_score	Score a note's outcome (correct/partially/wrong/irrelevant)
Data & Infrastructure
Tool	What It Does
emerson_connector_status	Health check for all data connectors
emerson_test_connector	Test a specific connector
emerson_fred_history	Historical FRED data for backtesting
Architecture
emerson_mcp/ ├── server.py # FastMCP server + all 25 tool registrations ├── requirements.txt ├── connectors/ │ ├── base.py # BaseConnector interface (pluggable pattern) │ ├── fred.py # FRED API (10 core macro series) │ ├── alpha_vantage.py # Alpha Vantage (equities, FX) with rate limiting │ └── edgar.py # SEC EDGAR (filings, XBRL facts, full-text search) ├── models/ │ ├── indicators.py # Indicator definitions, thresholds, composites │ ├── regime.py # State machine (pure function, anti-whipsaw) │ ├── thesis.py # Thesis lifecycle (Spark → Fortress → Closed) │ ├── calendar.py # Catalyst calendar (23 seeded events, monitoring windows) │ └── field_notes.py # Field Notes / Ledger (observation capture + scoring) └── README.md

Design Decisions
Pure functions for classification: The regime state machine has zero side effects. Indicators in, state out. No UI coupling, no DB writes. This is testable and portable.
Pluggable connectors: Adding a new data source means implementing BaseConnector. No existing code modified.
In-memory thesis store: For the test bed, theses live in memory. The interface is designed so swapping in PostgreSQL changes one file.
Rate limiting enforced in connector layer: Alpha Vantage free tier (5 calls/min) is handled automatically.
Anti-whipsaw logic: Regime state can only move one step per evaluation cycle. No RISK_ON → CRISIS in a single day.
Regime Composites
The 13 indicators roll up into 7 composites:

Composite	Indicators	What It Measures
rates	DGS10, DGS2, DFF, SOFR	Interest rate stress
liquidity	RRPONTSYD, WTREGEN	System plumbing pressure
credit	HY OAS, HYG	Credit market stress
volatility	VIX	Market fear
commodities	WTI	Energy/inflation pressure
fx	DXY, USD/JPY	Dollar strength / carry trade stress
banking	KRE	Regional bank health
What's Next
[ ] SQLite persistence (theses, notes, calendar survive restarts)
[ ] TradingView webhook receiver (price-level alerts)
[ ] Conjunction logic rules engine (if A AND B but not C)
[ ] Content token detection for earnings transcripts
[ ] Historical regime backtesting engine
[ ] Debrief card auto-generation after scheduled events
[ ] Streamable HTTP transport for mobile access
[ ] Credit Cascade visualization tools# Emerson
