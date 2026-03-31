"""
Emerson MCP Server — Test Bed

A parallel implementation of Emerson's analytical engine as an MCP server.
This gives Claude direct tool access to the data layer, regime state machine,
thesis lifecycle, and EDGAR research without waiting for the full frontend build.

Transport:
  - Default: stdio (works with Claude Desktop / Claude Code)
  - Switch to HTTP: change mcp.run() call at bottom

Environment variables:
  FRED_API_KEY         — Get free at https://fred.stlouisfed.org/docs/api/api_key.html
  ALPHA_VANTAGE_API_KEY — Get free at https://www.alphavantage.co/support/#api-key
  EDGAR_USER_AGENT     — Required by SEC (format: "App Name email@domain.com")
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from enum import Enum

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

from models import (
    ALL_INDICATORS, INDICATOR_MAP, FRED_INDICATORS, ALPHA_VANTAGE_INDICATORS,
    RegimeState, IndicatorReading, evaluate_regime,
    ThesisStage, ConvictionLevel, ThesisStore, Thesis,
    FlagColor,
    CalendarStore, EventType, CatalystEvent,
    FieldNotesStore, NoteType, OutcomeScore, FieldNote,
)
from connectors import FredConnector, AlphaVantageConnector, EdgarConnector


# ──────────────────────────────────────────────────────────
# Lifespan: initialize connectors and stores
# ──────────────────────────────────────────────────────────

@asynccontextmanager
async def app_lifespan():
    """Initialize all connectors and shared state."""
    fred = FredConnector()
    av = AlphaVantageConnector()
    edgar = EdgarConnector()
    thesis_store = ThesisStore()
    calendar_store = CalendarStore()
    field_notes_store = FieldNotesStore()

    # Shared state for regime tracking
    state = {
        "fred": fred,
        "av": av,
        "edgar": edgar,
        "thesis_store": thesis_store,
        "calendar_store": calendar_store,
        "field_notes_store": field_notes_store,
        "last_regime": None,  # Track previous regime for anti-whipsaw
        "last_readings": [],  # Cache last set of readings
    }

    yield state

    # Cleanup
    await fred.close()
    await av.close()
    await edgar.close()


mcp = FastMCP("emerson_mcp", lifespan=app_lifespan)


# ──────────────────────────────────────────────────────────
# Helper: get lifespan state from context
# ──────────────────────────────────────────────────────────

def _state(ctx):
    return ctx.request_context.lifespan_state


# ══════════════════════════════════════════════════════════
# TOOL GROUP 1: Connector Status
# ══════════════════════════════════════════════════════════

class ConnectorTestInput(BaseModel):
    """Input for testing a specific connector."""
    model_config = ConfigDict(extra="forbid")
    connector: str = Field(
        ...,
        description="Which connector to test: 'fred', 'alpha_vantage', or 'edgar'",
    )


@mcp.tool(
    name="emerson_test_connector",
    annotations={
        "title": "Test Data Connector",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def emerson_test_connector(params: ConnectorTestInput, ctx=None) -> str:
    """Test connectivity to a data source (FRED, Alpha Vantage, or EDGAR).
    Returns connection status, last sync time, and any errors."""
    s = _state(ctx)
    connectors = {"fred": s["fred"], "av": s["av"], "alpha_vantage": s["av"], "edgar": s["edgar"]}
    conn = connectors.get(params.connector)
    if not conn:
        return json.dumps({"error": f"Unknown connector: {params.connector}. Options: fred, alpha_vantage, edgar"})
    status = await conn.test_connection()
    return json.dumps(status.model_dump(), default=str, indent=2)


@mcp.tool(
    name="emerson_connector_status",
    annotations={
        "title": "All Connector Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_connector_status(ctx=None) -> str:
    """Show the current status of all data connectors — which are connected,
    last sync times, and any errors. Good first call to check system health."""
    s = _state(ctx)
    statuses = {
        "fred": s["fred"].status().model_dump(),
        "alpha_vantage": s["av"].status().model_dump(),
        "edgar": s["edgar"].status().model_dump(),
    }

    # Check for API keys
    env_status = {
        "FRED_API_KEY": "set" if os.environ.get("FRED_API_KEY") else "MISSING",
        "ALPHA_VANTAGE_API_KEY": "set" if os.environ.get("ALPHA_VANTAGE_API_KEY") else "MISSING",
    }
    return json.dumps({"connectors": statuses, "environment": env_status}, default=str, indent=2)


# ══════════════════════════════════════════════════════════
# TOOL GROUP 2: Regime Monitor
# ══════════════════════════════════════════════════════════

@mcp.tool(
    name="emerson_fetch_indicators",
    annotations={
        "title": "Fetch All Regime Indicators",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def emerson_fetch_indicators(ctx=None) -> str:
    """Fetch current values for all regime monitor indicators from FRED and
    Alpha Vantage. Returns raw values, timestamps, and staleness flags.
    This is the data-gathering step before regime evaluation."""
    s = _state(ctx)
    fred: FredConnector = s["fred"]
    av: AlphaVantageConnector = s["av"]

    results = []
    errors = []

    # Fetch FRED indicators
    fred_series = [ind.series_id for ind in FRED_INDICATORS]
    fred_data = await fred.fetch_batch(fred_series)
    for dp in fred_data:
        # Find the matching indicator def
        matching = [ind for ind in FRED_INDICATORS if ind.series_id == dp.series_id]
        if matching:
            ind = matching[0]
            age_hours = (datetime.utcnow() - dp.timestamp).total_seconds() / 3600
            results.append({
                "id": ind.id,
                "name": ind.name,
                "series_id": dp.series_id,
                "value": dp.value,
                "unit": ind.unit,
                "source": "fred",
                "timestamp": dp.timestamp.isoformat(),
                "age_hours": round(age_hours, 1),
                "is_stale": age_hours > 48,
                "composite": ind.composite,
            })

    if fred.status().error:
        errors.append(f"FRED: {fred.status().error}")

    # Fetch Alpha Vantage indicators
    av_series = [ind.series_id for ind in ALPHA_VANTAGE_INDICATORS]
    av_data = await av.fetch_batch(av_series)
    for dp in av_data:
        matching = [ind for ind in ALPHA_VANTAGE_INDICATORS if ind.series_id == dp.series_id]
        if matching:
            ind = matching[0]
            age_hours = (datetime.utcnow() - dp.timestamp).total_seconds() / 3600
            results.append({
                "id": ind.id,
                "name": ind.name,
                "series_id": dp.series_id,
                "value": dp.value,
                "unit": ind.unit,
                "source": "alpha_vantage",
                "timestamp": dp.timestamp.isoformat(),
                "age_hours": round(age_hours, 1),
                "is_stale": age_hours > 48,
                "composite": ind.composite,
            })

    if av.status().error:
        errors.append(f"Alpha Vantage: {av.status().error}")

    # Cache readings for regime evaluation
    readings = []
    for r in results:
        readings.append(IndicatorReading(
            indicator_id=r["id"],
            value=r["value"],
            timestamp=datetime.fromisoformat(r["timestamp"]),
            source=r["source"],
            is_stale=r["is_stale"],
        ))
    s["last_readings"] = readings

    return json.dumps({
        "indicators": results,
        "total_fetched": len(results),
        "total_expected": len(ALL_INDICATORS),
        "errors": errors,
        "fetched_at": datetime.utcnow().isoformat(),
    }, indent=2)


@mcp.tool(
    name="emerson_evaluate_regime",
    annotations={
        "title": "Evaluate Regime State",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_evaluate_regime(ctx=None) -> str:
    """Run the regime state machine on the most recently fetched indicators.
    Returns the five-state classification (risk_on → crisis), composite scores
    for each sector, individual indicator flags (four-tier heat map), and the
    aggregate stress score. Includes anti-whipsaw logic.

    Call emerson_fetch_indicators first to get fresh data."""
    s = _state(ctx)
    readings = s.get("last_readings", [])

    if not readings:
        return json.dumps({
            "error": "No indicator readings available. Call emerson_fetch_indicators first.",
        })

    previous = s.get("last_regime")
    assessment = evaluate_regime(readings, INDICATOR_MAP, previous)
    s["last_regime"] = assessment.state

    # Format for readability
    output = {
        "regime_state": assessment.state.value,
        "previous_state": assessment.previous_state.value if assessment.previous_state else None,
        "stress_score": assessment.stress_score,
        "confidence": assessment.confidence,
        "anti_whipsaw_note": assessment.anti_whipsaw_note,
        "composites": [
            {
                "name": c.composite,
                "score": c.score,
                "dominant_flag": c.dominant_flag.value,
                "flags": c.flag_counts,
            }
            for c in assessment.composite_scores
        ],
        "indicator_flags": [
            {
                "id": f.indicator_id,
                "name": f.name,
                "value": f.value,
                "flag": f.flag.value,
                "composite": f.composite,
                "is_stale": f.is_stale,
            }
            for f in assessment.indicator_flags
        ],
        "evaluated_at": assessment.evaluated_at.isoformat(),
    }
    return json.dumps(output, indent=2)


class IndicatorDefinitionsInput(BaseModel):
    """Input for listing indicator definitions."""
    model_config = ConfigDict(extra="forbid")
    composite: Optional[str] = Field(
        default=None,
        description="Filter by composite name (e.g., 'rates', 'credit', 'liquidity', 'fx', 'volatility', 'commodities', 'banking')",
    )


@mcp.tool(
    name="emerson_list_indicators",
    annotations={
        "title": "List Indicator Definitions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_list_indicators(params: IndicatorDefinitionsInput) -> str:
    """List all monitored indicators with their thresholds, sources, composites,
    and update frequencies. Useful for understanding what the regime monitor tracks."""
    indicators = ALL_INDICATORS
    if params.composite:
        indicators = [i for i in indicators if i.composite == params.composite]

    output = [
        {
            "id": i.id,
            "name": i.name,
            "series_id": i.series_id,
            "source": i.source,
            "composite": i.composite,
            "direction": i.direction.value,
            "thresholds": {
                "watch": i.threshold_watch,
                "elevated": i.threshold_elevated,
                "stress": i.threshold_stress,
            },
            "unit": i.unit,
            "update_frequency": i.update_frequency,
        }
        for i in indicators
    ]
    return json.dumps({"indicators": output, "count": len(output)}, indent=2)


# ══════════════════════════════════════════════════════════
# TOOL GROUP 3: Morning Scrum
# ══════════════════════════════════════════════════════════

@mcp.tool(
    name="emerson_morning_scrum",
    annotations={
        "title": "Morning Scrum — Daily Briefing",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def emerson_morning_scrum(ctx=None) -> str:
    """Generate the Morning Scrum daily command center view. This is the primary
    entry point — it fetches fresh indicator data, runs the regime state machine,
    checks for stale theses, shows upcoming catalysts and monitoring-window events,
    and assembles an aggregated briefing.

    This is what Michael sees first each morning."""
    s = _state(ctx)
    fred: FredConnector = s["fred"]
    av: AlphaVantageConnector = s["av"]
    thesis_store: ThesisStore = s["thesis_store"]
    calendar_store: CalendarStore = s["calendar_store"]
    field_notes_store: FieldNotesStore = s["field_notes_store"]

    briefing = {
        "generated_at": datetime.utcnow().isoformat(),
        "sections": {},
    }

    # 1. Fetch indicators
    all_readings = []
    fetch_errors = []

    fred_series = [ind.series_id for ind in FRED_INDICATORS]
    fred_data = await fred.fetch_batch(fred_series)
    for dp in fred_data:
        matching = [ind for ind in FRED_INDICATORS if ind.series_id == dp.series_id]
        if matching:
            all_readings.append(IndicatorReading(
                indicator_id=matching[0].id,
                value=dp.value,
                timestamp=dp.timestamp,
                source="fred",
                is_stale=(datetime.utcnow() - dp.timestamp).total_seconds() > 172800,
            ))
    if fred.status().error:
        fetch_errors.append(f"FRED: {fred.status().error}")

    av_series = [ind.series_id for ind in ALPHA_VANTAGE_INDICATORS]
    av_data = await av.fetch_batch(av_series)
    for dp in av_data:
        matching = [ind for ind in ALPHA_VANTAGE_INDICATORS if ind.series_id == dp.series_id]
        if matching:
            all_readings.append(IndicatorReading(
                indicator_id=matching[0].id,
                value=dp.value,
                timestamp=dp.timestamp,
                source="alpha_vantage",
                is_stale=(datetime.utcnow() - dp.timestamp).total_seconds() > 172800,
            ))
    if av.status().error:
        fetch_errors.append(f"Alpha Vantage: {av.status().error}")

    s["last_readings"] = all_readings

    # 2. Regime assessment
    previous = s.get("last_regime")
    if all_readings:
        assessment = evaluate_regime(all_readings, INDICATOR_MAP, previous)
        s["last_regime"] = assessment.state

        briefing["sections"]["regime"] = {
            "state": assessment.state.value,
            "stress_score": assessment.stress_score,
            "confidence": assessment.confidence,
            "composites": {
                c.composite: {
                    "score": c.score,
                    "dominant_flag": c.dominant_flag.value,
                }
                for c in assessment.composite_scores
            },
            "anti_whipsaw_note": assessment.anti_whipsaw_note,
        }

        # Fired alerts: any indicator at elevated or stress
        fired = [
            {
                "indicator": f.name,
                "value": f.value,
                "flag": f.flag.value,
                "composite": f.composite,
            }
            for f in assessment.indicator_flags
            if f.flag in (FlagColor.ELEVATED, FlagColor.STRESS)
        ]
        briefing["sections"]["fired_alerts"] = {
            "count": len(fired),
            "alerts": fired,
        }

        # Stale data warnings
        stale = [
            {"indicator": f.name, "value": f.value}
            for f in assessment.indicator_flags if f.is_stale
        ]
        briefing["sections"]["stale_data"] = {
            "count": len(stale),
            "warnings": stale,
        }
    else:
        briefing["sections"]["regime"] = {
            "error": "No indicator data fetched. Check API keys and connector status.",
            "fetch_errors": fetch_errors,
        }

    # 3. Thesis overview
    all_theses = thesis_store.list_all()
    stale_theses = thesis_store.stale_theses(days=14)
    active_theses = [t for t in all_theses if t.stage in (ThesisStage.ACTIVE, ThesisStage.FORTRESS)]

    briefing["sections"]["theses"] = {
        "total": len(all_theses),
        "active": len(active_theses),
        "stale_count": len(stale_theses),
        "stale_theses": [
            {
                "id": t.id,
                "title": t.title,
                "stage": t.stage.value,
                "days_since_update": (datetime.utcnow() - t.updated_at).days,
            }
            for t in stale_theses
        ],
        "active_summary": [
            {
                "id": t.id,
                "title": t.title,
                "stage": t.stage.value,
                "conviction": t.conviction.value,
                "tickers": t.tickers,
            }
            for t in active_theses
        ],
    }

    # 4. Catalyst calendar — upcoming events and monitoring window
    upcoming = calendar_store.list_upcoming(days=14)
    in_window = calendar_store.in_monitoring_window()
    briefing["sections"]["catalyst_calendar"] = {
        "upcoming_14_days": [
            {
                "id": e.id,
                "date": e.date,
                "label": e.label,
                "ticker": e.ticker,
                "type": e.event_type.value,
                "linked_theses": e.linked_theses,
            }
            for e in upcoming
        ],
        "monitoring_window_active": [
            {
                "id": e.id,
                "date": e.date,
                "label": e.label,
                "ticker": e.ticker,
                "type": e.event_type.value,
                "note": "Weather layer ACTIVE — monitoring mode engaged",
            }
            for e in in_window
        ],
        "upcoming_count": len(upcoming),
        "active_monitoring_count": len(in_window),
    }

    # 5. Field notes summary
    briefing["sections"]["field_notes"] = {
        "total_notes": field_notes_store.count(),
        "unscored_count": len(field_notes_store.unscored()),
    }

    # 6. Stat card summary (mirrors the five stat cards in the Scrum prototype)
    briefing["sections"]["stat_cards"] = {
        "active_theses": len(active_theses),
        "fired_alerts": len(briefing["sections"].get("fired_alerts", {}).get("alerts", [])),
        "stale_theses": len(stale_theses),
        "field_notes": field_notes_store.count(),
        "upcoming_catalysts": len(upcoming),
    }

    # 7. Fetch errors
    if fetch_errors:
        briefing["sections"]["system_warnings"] = fetch_errors

    return json.dumps(briefing, indent=2)


# ══════════════════════════════════════════════════════════
# TOOL GROUP 4: Thesis Registry
# ══════════════════════════════════════════════════════════

class CreateThesisInput(BaseModel):
    """Input for creating a new thesis."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., description="Thesis title (e.g., 'Regional bank stress play via KRE puts')", min_length=3, max_length=200)
    summary: str = Field(default="", description="Brief thesis description")
    tickers: list[str] = Field(default_factory=list, description="Related tickers (e.g., ['KRE', 'NYCB'])")
    sector: str = Field(default="", description="Sector classification")
    conviction: str = Field(default="low", description="Conviction level: low, moderate, high, very_high")
    entry_criteria: str = Field(default="", description="What conditions justify entry")
    exit_criteria: str = Field(default="", description="What conditions justify exit")
    linked_indicator_ids: list[str] = Field(default_factory=list, description="Emerson indicator IDs to link (e.g., ['fred_hy_oas', 'av_kre'])")


@mcp.tool(
    name="emerson_create_thesis",
    annotations={
        "title": "Create New Thesis",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_create_thesis(params: CreateThesisInput, ctx=None) -> str:
    """Create a new investment thesis. Starts in SPARK stage.
    Link it to regime indicators to enable cross-module alerts."""
    s = _state(ctx)
    store: ThesisStore = s["thesis_store"]

    try:
        conviction = ConvictionLevel(params.conviction)
    except ValueError:
        conviction = ConvictionLevel.LOW

    thesis = store.create(
        title=params.title,
        summary=params.summary,
        tickers=params.tickers,
        sector=params.sector,
        conviction=conviction,
        entry_criteria=params.entry_criteria,
        exit_criteria=params.exit_criteria,
        linked_indicator_ids=params.linked_indicator_ids,
    )
    return json.dumps({
        "created": True,
        "thesis": thesis.model_dump(),
    }, default=str, indent=2)


class ListThesesInput(BaseModel):
    """Input for listing theses."""
    model_config = ConfigDict(extra="forbid")
    stage: Optional[str] = Field(
        default=None,
        description="Filter by stage: spark, draft, active, fortress, closed",
    )


@mcp.tool(
    name="emerson_list_theses",
    annotations={
        "title": "List Theses",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_list_theses(params: ListThesesInput, ctx=None) -> str:
    """List all investment theses, optionally filtered by lifecycle stage."""
    s = _state(ctx)
    store: ThesisStore = s["thesis_store"]

    stage = None
    if params.stage:
        try:
            stage = ThesisStage(params.stage)
        except ValueError:
            return json.dumps({"error": f"Invalid stage: {params.stage}. Options: spark, draft, active, fortress, closed"})

    theses = store.list_all(stage=stage)
    return json.dumps({
        "theses": [t.model_dump() for t in theses],
        "count": len(theses),
    }, default=str, indent=2)


class GetThesisInput(BaseModel):
    """Input for getting a single thesis."""
    model_config = ConfigDict(extra="forbid")
    thesis_id: str = Field(..., description="Thesis ID (e.g., 'thesis_0001')")


@mcp.tool(
    name="emerson_get_thesis",
    annotations={
        "title": "Get Thesis Detail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_get_thesis(params: GetThesisInput, ctx=None) -> str:
    """Get full details for a specific thesis including assumptions,
    linked indicators, entry/exit criteria, and version history."""
    s = _state(ctx)
    store: ThesisStore = s["thesis_store"]
    thesis = store.get(params.thesis_id)
    if not thesis:
        return json.dumps({"error": f"Thesis {params.thesis_id} not found"})
    return json.dumps(thesis.model_dump(), default=str, indent=2)


class TransitionThesisInput(BaseModel):
    """Input for transitioning a thesis stage."""
    model_config = ConfigDict(extra="forbid")
    thesis_id: str = Field(..., description="Thesis ID")
    target_stage: str = Field(..., description="Target stage: spark, draft, active, fortress, closed")
    reason: str = Field(default="", description="Reason for transition (especially important when closing)")


@mcp.tool(
    name="emerson_transition_thesis",
    annotations={
        "title": "Transition Thesis Stage",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_transition_thesis(params: TransitionThesisInput, ctx=None) -> str:
    """Advance or revert a thesis through its lifecycle:
    Spark → Draft → Active → Fortress → Closed.
    Validates that the transition is legal per the state machine."""
    s = _state(ctx)
    store: ThesisStore = s["thesis_store"]
    try:
        target = ThesisStage(params.target_stage)
    except ValueError:
        return json.dumps({"error": f"Invalid stage: {params.target_stage}"})

    try:
        thesis = store.transition(params.thesis_id, target, params.reason)
        return json.dumps({
            "transitioned": True,
            "thesis": thesis.model_dump(),
        }, default=str, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


class UpdateThesisInput(BaseModel):
    """Input for updating thesis fields."""
    model_config = ConfigDict(extra="forbid")
    thesis_id: str = Field(..., description="Thesis ID")
    title: Optional[str] = Field(default=None, description="Updated title")
    summary: Optional[str] = Field(default=None, description="Updated summary")
    conviction: Optional[str] = Field(default=None, description="Updated conviction: low, moderate, high, very_high")
    tickers: Optional[list[str]] = Field(default=None, description="Updated ticker list")
    entry_criteria: Optional[str] = Field(default=None, description="Updated entry criteria")
    exit_criteria: Optional[str] = Field(default=None, description="Updated exit criteria")
    note: Optional[str] = Field(default=None, description="Add a note to the thesis")


@mcp.tool(
    name="emerson_update_thesis",
    annotations={
        "title": "Update Thesis",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_update_thesis(params: UpdateThesisInput, ctx=None) -> str:
    """Update fields on an existing thesis. Only provided fields are changed.
    Adding a note appends to the thesis note history."""
    s = _state(ctx)
    store: ThesisStore = s["thesis_store"]

    thesis = store.get(params.thesis_id)
    if not thesis:
        return json.dumps({"error": f"Thesis {params.thesis_id} not found"})

    updates = {}
    if params.title is not None:
        updates["title"] = params.title
    if params.summary is not None:
        updates["summary"] = params.summary
    if params.conviction is not None:
        try:
            updates["conviction"] = ConvictionLevel(params.conviction)
        except ValueError:
            return json.dumps({"error": f"Invalid conviction: {params.conviction}"})
    if params.tickers is not None:
        updates["tickers"] = params.tickers
    if params.entry_criteria is not None:
        updates["entry_criteria"] = params.entry_criteria
    if params.exit_criteria is not None:
        updates["exit_criteria"] = params.exit_criteria
    if params.note is not None:
        updates["notes"] = thesis.notes + [f"[{datetime.utcnow().isoformat()}] {params.note}"]

    updated = store.update(params.thesis_id, **updates)
    return json.dumps({
        "updated": True,
        "thesis": updated.model_dump(),
    }, default=str, indent=2)


# ══════════════════════════════════════════════════════════
# TOOL GROUP 5: EDGAR Research
# ══════════════════════════════════════════════════════════

class EdgarFilingsInput(BaseModel):
    """Input for EDGAR filing lookup."""
    model_config = ConfigDict(extra="forbid")
    cik: str = Field(..., description="Company CIK number (e.g., '0000320193' for Apple, '886982' for NYCB)")
    form_type: str = Field(default="", description="Filter by form type: '10-K', '10-Q', '8-K', 'DEF 14A', etc.")
    limit: int = Field(default=10, description="Max filings to return", ge=1, le=50)


@mcp.tool(
    name="emerson_edgar_filings",
    annotations={
        "title": "EDGAR — Company Filings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def emerson_edgar_filings(params: EdgarFilingsInput, ctx=None) -> str:
    """Look up SEC filings for a company by CIK number. Returns recent filings
    with form type, date, accession number, and direct link. Filter by form type
    (10-K, 8-K, etc.) for targeted research."""
    s = _state(ctx)
    edgar: EdgarConnector = s["edgar"]
    result = await edgar.get_company_filings(params.cik, params.form_type, params.limit)
    return json.dumps(result, indent=2)


class EdgarFactsInput(BaseModel):
    """Input for EDGAR company facts."""
    model_config = ConfigDict(extra="forbid")
    cik: str = Field(..., description="Company CIK number")
    fact_name: str = Field(
        default="",
        description="Specific XBRL fact (e.g., 'Revenues', 'Assets', 'us-gaap/CashAndCashEquivalentsAtCarryingValue'). Leave empty to list available facts.",
    )


@mcp.tool(
    name="emerson_edgar_facts",
    annotations={
        "title": "EDGAR — Company Financial Facts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def emerson_edgar_facts(params: EdgarFactsInput, ctx=None) -> str:
    """Extract structured financial data from EDGAR XBRL filings. More reliable
    than HTML/PDF parsing for balance sheet, income statement, and cash flow items.
    Use for cash runway calculations, dilution forensics, and fundamental analysis."""
    s = _state(ctx)
    edgar: EdgarConnector = s["edgar"]
    result = await edgar.get_company_facts(params.cik, params.fact_name)
    return json.dumps(result, default=str, indent=2)


class EdgarSearchInput(BaseModel):
    """Input for EDGAR full-text search."""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., description="Search terms (e.g., 'covenant waiver', 'going concern', 'material weakness')", min_length=2)
    form_type: str = Field(default="", description="Filter by form type")
    limit: int = Field(default=10, ge=1, le=50)


@mcp.tool(
    name="emerson_edgar_search",
    annotations={
        "title": "EDGAR — Full-Text Filing Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def emerson_edgar_search(params: EdgarSearchInput, ctx=None) -> str:
    """Search across all SEC filings by keyword. Useful for finding companies
    mentioning specific terms like 'going concern', 'covenant waiver',
    'material weakness', or 'goodwill impairment'. Returns company name,
    form type, date, and description."""
    s = _state(ctx)
    edgar: EdgarConnector = s["edgar"]
    result = await edgar.search_filings(params.query, params.form_type, params.limit)
    return json.dumps(result, default=str, indent=2)


# ══════════════════════════════════════════════════════════
# TOOL GROUP 6: FRED Historical Data (for backtest prep)
# ══════════════════════════════════════════════════════════

class FredHistoryInput(BaseModel):
    """Input for FRED historical data fetch."""
    model_config = ConfigDict(extra="forbid")
    series_id: str = Field(..., description="FRED series ID (e.g., 'DGS10', 'BAMLH0A0HYM2')")
    start_date: str = Field(default="2020-01-01", description="Start date (YYYY-MM-DD)")
    end_date: str = Field(default="", description="End date (YYYY-MM-DD), empty for latest")


@mcp.tool(
    name="emerson_fred_history",
    annotations={
        "title": "FRED — Historical Data",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def emerson_fred_history(params: FredHistoryInput, ctx=None) -> str:
    """Fetch historical time series from FRED for backtesting and trend analysis.
    Returns date-value pairs for the specified range. Use for regime backtesting,
    threshold calibration, and historical context."""
    s = _state(ctx)
    fred: FredConnector = s["fred"]
    data = await fred.fetch_history(params.series_id, params.start_date, params.end_date)

    if not data:
        return json.dumps({"error": f"No historical data for {params.series_id}. Check series ID and FRED_API_KEY."})

    return json.dumps({
        "series_id": params.series_id,
        "start": params.start_date,
        "end": params.end_date or "latest",
        "count": len(data),
        "data": [
            {"date": dp.timestamp.strftime("%Y-%m-%d"), "value": dp.value}
            for dp in data
        ],
    }, indent=2)


# ══════════════════════════════════════════════════════════
# TOOL GROUP 7: Catalyst Calendar
# ══════════════════════════════════════════════════════════

class CalendarUpcomingInput(BaseModel):
    """Input for upcoming calendar events."""
    model_config = ConfigDict(extra="forbid")
    days: int = Field(default=14, description="Number of days ahead to look", ge=1, le=180)


@mcp.tool(
    name="emerson_calendar_upcoming",
    annotations={
        "title": "Calendar — Upcoming Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_calendar_upcoming(params: CalendarUpcomingInput, ctx=None) -> str:
    """List upcoming catalyst events for the next N days. Shows FOMC meetings,
    BoJ decisions, CPI/PCE prints, earnings dates, treasury events, and custom
    catalysts. Includes which theses each event is linked to."""
    s = _state(ctx)
    store: CalendarStore = s["calendar_store"]
    events = store.list_upcoming(days=params.days)
    return json.dumps({
        "events": [e.model_dump() for e in events],
        "count": len(events),
        "window_days": params.days,
    }, default=str, indent=2)


@mcp.tool(
    name="emerson_calendar_monitoring",
    annotations={
        "title": "Calendar — Active Monitoring Window",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_calendar_monitoring(ctx=None) -> str:
    """Show events currently within their monitoring window — the Weather layer
    is active for these. Typically 24-48 hours before the event. When active,
    the trigger system shifts from passive to active content token detection."""
    s = _state(ctx)
    store: CalendarStore = s["calendar_store"]
    events = store.in_monitoring_window()
    return json.dumps({
        "monitoring_active": [
            {
                "id": e.id,
                "date": e.date,
                "label": e.label,
                "ticker": e.ticker,
                "type": e.event_type.value,
                "linked_theses": e.linked_theses,
                "monitoring_window_hours": e.monitoring_window_hours,
                "status": "WEATHER LAYER ACTIVE",
            }
            for e in events
        ],
        "count": len(events),
    }, indent=2)


class CalendarByThesisInput(BaseModel):
    """Input for thesis-linked calendar lookup."""
    model_config = ConfigDict(extra="forbid")
    thesis_name: str = Field(..., description="Thesis name to find linked events (e.g., 'carry_unwind', 'debasement', 'private_credit', 'defense', 'hormuz')")


@mcp.tool(
    name="emerson_calendar_by_thesis",
    annotations={
        "title": "Calendar — Events by Thesis",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_calendar_by_thesis(params: CalendarByThesisInput, ctx=None) -> str:
    """Find all calendar events linked to a specific thesis. Useful for seeing
    the full catalyst timeline for a particular investment idea."""
    s = _state(ctx)
    store: CalendarStore = s["calendar_store"]
    events = store.by_thesis(params.thesis_name)
    return json.dumps({
        "thesis": params.thesis_name,
        "events": [e.model_dump() for e in events],
        "count": len(events),
    }, default=str, indent=2)


class AddCalendarEventInput(BaseModel):
    """Input for adding a calendar event."""
    model_config = ConfigDict(extra="forbid")
    date: str = Field(..., description="Event date YYYY-MM-DD")
    label: str = Field(..., description="Event description", min_length=3, max_length=300)
    ticker: str = Field(default="", description="Associated ticker or macro label")
    event_type: str = Field(default="custom", description="Event type: fed, boj, ecb, macro, earnings, credit, treasury, energy, filing, custom")
    linked_theses: list[str] = Field(default_factory=list, description="Thesis names to link")
    notes: str = Field(default="", description="Additional context")


@mcp.tool(
    name="emerson_calendar_add",
    annotations={
        "title": "Calendar — Add Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_calendar_add(params: AddCalendarEventInput, ctx=None) -> str:
    """Add a new catalyst event to the calendar. Link it to theses so the
    Morning Scrum can show relevant upcoming catalysts per thesis."""
    s = _state(ctx)
    store: CalendarStore = s["calendar_store"]
    try:
        evt = store.add(
            date=params.date, label=params.label, ticker=params.ticker,
            event_type=params.event_type, linked_theses=params.linked_theses,
            notes=params.notes,
        )
        return json.dumps({"created": True, "event": evt.model_dump()}, default=str, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


class AddDebriefInput(BaseModel):
    """Input for adding a post-event debrief."""
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(..., description="Calendar event ID (e.g., 'evt_0004')")
    debrief: str = Field(..., description="Post-event debrief text: what happened, delta from expectations, thesis impact", min_length=10)


@mcp.tool(
    name="emerson_calendar_debrief",
    annotations={
        "title": "Calendar — Add Event Debrief",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_calendar_debrief(params: AddDebriefInput, ctx=None) -> str:
    """Add a post-event debrief to a completed catalyst event. Captures what happened,
    delta from expectations, market reaction, and thesis impact. These appear in
    the next morning's Scrum."""
    s = _state(ctx)
    store: CalendarStore = s["calendar_store"]
    evt = store.add_debrief(params.event_id, params.debrief)
    if evt:
        return json.dumps({"debriefed": True, "event": evt.model_dump()}, default=str, indent=2)
    return json.dumps({"error": f"Event {params.event_id} not found"})


# ══════════════════════════════════════════════════════════
# TOOL GROUP 8: Field Notes — The Ledger
# ══════════════════════════════════════════════════════════

class CreateFieldNoteInput(BaseModel):
    """Input for creating a field note."""
    model_config = ConfigDict(extra="forbid")
    content: str = Field(..., description="The observation text", min_length=5, max_length=5000)
    note_type: str = Field(default="idea", description="Type: chart, reading, idea, debrief")
    tickers: list[str] = Field(default_factory=list, description="Tagged tickers (e.g., ['KRE', 'NYCB'])")
    linked_thesis_ids: list[str] = Field(default_factory=list, description="Thesis IDs to link")
    source: str = Field(default="", description="Source: chart timeframe, article URL, filing reference")
    tags: list[str] = Field(default_factory=list, description="Free-form tags for search")


@mcp.tool(
    name="emerson_note_create",
    annotations={
        "title": "Field Notes — Log Observation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_note_create(params: CreateFieldNoteInput, ctx=None) -> str:
    """Log a field note — a chart observation, reading spark, or spontaneous insight.
    Tag with tickers and link to theses. This is the raw material for Emerson's
    memory system and the Ledger scoring pipeline."""
    s = _state(ctx)
    store: FieldNotesStore = s["field_notes_store"]
    note = store.create(
        content=params.content,
        note_type=params.note_type,
        tickers=params.tickers,
        linked_thesis_ids=params.linked_thesis_ids,
        source=params.source,
        tags=params.tags,
    )
    return json.dumps({"created": True, "note": note.model_dump()}, default=str, indent=2)


class ListFieldNotesInput(BaseModel):
    """Input for listing field notes."""
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=100)


@mcp.tool(
    name="emerson_note_list",
    annotations={
        "title": "Field Notes — List Recent",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_note_list(params: ListFieldNotesInput, ctx=None) -> str:
    """List recent field notes, most recent first."""
    s = _state(ctx)
    store: FieldNotesStore = s["field_notes_store"]
    notes = store.list_all(limit=params.limit)
    return json.dumps({
        "notes": [n.model_dump() for n in notes],
        "count": len(notes),
        "total": store.count(),
    }, default=str, indent=2)


class SearchFieldNotesInput(BaseModel):
    """Input for searching field notes."""
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., description="Search text — matches against content, tickers, tags, and source", min_length=1)


@mcp.tool(
    name="emerson_note_search",
    annotations={
        "title": "Field Notes — Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_note_search(params: SearchFieldNotesInput, ctx=None) -> str:
    """Search field notes by keyword. Matches against content, tickers, tags,
    and source. This is the retrieval side of the memory system — find past
    observations before they're forgotten."""
    s = _state(ctx)
    store: FieldNotesStore = s["field_notes_store"]
    results = store.search(params.query)
    return json.dumps({
        "query": params.query,
        "results": [n.model_dump() for n in results],
        "count": len(results),
    }, default=str, indent=2)


class NoteByTickerInput(BaseModel):
    """Input for notes by ticker."""
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(..., description="Ticker symbol (e.g., 'KRE', 'ARCC')")


@mcp.tool(
    name="emerson_note_by_ticker",
    annotations={
        "title": "Field Notes — By Ticker",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def emerson_note_by_ticker(params: NoteByTickerInput, ctx=None) -> str:
    """Find all field notes tagged with a specific ticker. Essential for
    the 'Why am I watching this?' retrieval pattern."""
    s = _state(ctx)
    store: FieldNotesStore = s["field_notes_store"]
    results = store.by_ticker(params.ticker)
    return json.dumps({
        "ticker": params.ticker,
        "notes": [n.model_dump() for n in results],
        "count": len(results),
    }, default=str, indent=2)


class ScoreFieldNoteInput(BaseModel):
    """Input for scoring a field note outcome."""
    model_config = ConfigDict(extra="forbid")
    note_id: str = Field(..., description="Field note ID (e.g., 'fn_0001')")
    outcome: str = Field(..., description="Outcome: correct, partially, wrong, irrelevant")
    outcome_notes: str = Field(default="", description="What actually happened")


@mcp.tool(
    name="emerson_note_score",
    annotations={
        "title": "Field Notes — Score Outcome",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def emerson_note_score(params: ScoreFieldNoteInput, ctx=None) -> str:
    """Score a field note's outcome after the fact. This builds the Ledger —
    tracking which observations were correct, partially right, wrong, or
    irrelevant. Over time, this calibrates analytical confidence."""
    s = _state(ctx)
    store: FieldNotesStore = s["field_notes_store"]
    try:
        note = store.score(params.note_id, params.outcome, params.outcome_notes)
        if note:
            return json.dumps({"scored": True, "note": note.model_dump()}, default=str, indent=2)
        return json.dumps({"error": f"Note {params.note_id} not found"})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ──────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Default: stdio transport (works with Claude Desktop / Claude Code)
    # For phone access later, change to:
    #   mcp.run(transport="streamable_http", port=8000)
    mcp.run()
