"""Energy Tariff Assistant rate engine.

Pure Python: no Home Assistant imports, so it can be tested and reused on
its own. The integration wraps it; scripts and CI use it directly.
"""

from .demand import DemandMeter
from .forecast import build_forecast, build_intervals
from .meter import BUCKETS, Bucket, Ledger, bucket_start
from .model import FORMAT, VERSION, DemandCharge, Period, Plan, Stream, Tier, Window, load_plan
from .resolver import RateResult, Resolver
from .validate import ValidationResult, validate_plan

__all__ = [
    "FORMAT",
    "VERSION",
    "Plan",
    "Period",
    "Stream",
    "Tier",
    "Window",
    "DemandCharge",
    "DemandMeter",
    "load_plan",
    "RateResult",
    "Resolver",
    "build_forecast",
    "build_intervals",
    "BUCKETS",
    "Bucket",
    "Ledger",
    "bucket_start",
    "ValidationResult",
    "validate_plan",
]
