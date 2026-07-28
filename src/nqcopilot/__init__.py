"""nqcopilot — an intraday Nasdaq futures copilot with hard Apex rule enforcement.

Two layers, with deliberately different epistemic status:

* `signals` / `market` read the tape and propose trades. This is pattern
  recognition with modest historical edge. It is wrong regularly and cannot be
  made otherwise.
* `apex` enforces account rules and position sizing. This is arithmetic, it is
  exact, and it holds veto power over anything the signal layer proposes.

Start with `playbook.evaluate`, which returns a single `Directive`.
"""

from .apex import (
    APEX_50K_EVAL,
    APEX_50K_PA,
    PRESETS,
    AccountProfile,
    AccountState,
    Blocker,
    RiskEngine,
    RiskLimits,
    Severity,
    Sizing,
    TrailingMode,
    check_threshold_consistency,
    payout_status,
    ScalingLadder,
    ScalingTier,
)
from .backtest import BacktestConfig, BacktestResult, CompletedTrade, run_backtest
from .bars import Bar, Session, classify_session, trading_date
from .calendar import CalendarError, EconomicEvent, fetch_economic_calendar
from .contracts import ES, MES, MNQ, NQ, ContractSpec, get_contract
from .data import generate_demo_bars, load_csv
from .market import IndicatorConfig, MarketContext, Regime
from .playbook import Action, Directive, PlaybookConfig, evaluate
from .signals import Setup, detect_all
from .webhook import BarStore, WebhookConfig, WebhookContext, WebhookServer

__version__ = "1.0.0"

__all__ = [
    "Action",
    "APEX_50K_EVAL",
    "APEX_50K_PA",
    "AccountProfile",
    "AccountState",
    "BacktestConfig",
    "BacktestResult",
    "Bar",
    "BarStore",
    "Blocker",
    "CalendarError",
    "CompletedTrade",
    "ContractSpec",
    "EconomicEvent",
    "Directive",
    "ES",
    "IndicatorConfig",
    "MES",
    "MNQ",
    "MarketContext",
    "NQ",
    "PRESETS",
    "PlaybookConfig",
    "Regime",
    "RiskEngine",
    "RiskLimits",
    "Session",
    "Setup",
    "Severity",
    "WebhookConfig",
    "WebhookContext",
    "WebhookServer",
    "ScalingLadder",
    "ScalingTier",
    "Sizing",
    "TrailingMode",
    "classify_session",
    "check_threshold_consistency",
    "payout_status",
    "detect_all",
    "evaluate",
    "fetch_economic_calendar",
    "generate_demo_bars",
    "get_contract",
    "load_csv",
    "run_backtest",
    "trading_date",
    "__version__",
]
