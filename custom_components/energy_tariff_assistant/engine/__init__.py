"""Energy Tariff Assistant rate engine.

Pure Python: no Home Assistant imports, so it can be tested and reused on
its own. The integration wraps it; scripts and CI use it directly.
"""

from .model import FORMAT, VERSION, Plan, Period, Stream, Tier, Window, load_plan
from .resolver import RateResult, Resolver
from .forecast import build_forecast
from .validate import ValidationResult, validate_plan

__all__ = [
    "FORMAT",
    "VERSION",
    "Plan",
    "Period",
    "Stream",
    "Tier",
    "Window",
    "load_plan",
    "RateResult",
    "Resolver",
    "build_forecast",
    "ValidationResult",
    "validate_plan",
]
