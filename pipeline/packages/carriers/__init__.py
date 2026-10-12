"""
Carrier tracking adapters (UPS, USPS) behind one interface.

Design decisions:

- Everything shared (typed errors, OAuth token cache, rate limiter, retrying HTTP helper, the
  adapter registry) lives in base.py; ups.py and usps.py only map carrier payloads to TrackResult.
- Names are re-exported here so callers import from pipeline.packages.carriers.
"""
from pipeline.packages.carriers.base import (
    CarrierAdapter,
    CarrierAuthError,
    CarrierError,
    CarrierNotFound,
    CarrierRateLimited,
    CarrierUnavailable,
    OAuthClientCredentials,
    RateLimiter,
    TrackEvent,
    TrackResult,
    adapters,
    normalise_status,
    request_json,
)

__all__ = [
    "CarrierAdapter", "CarrierAuthError", "CarrierError", "CarrierNotFound", "CarrierRateLimited",
    "CarrierUnavailable", "OAuthClientCredentials", "RateLimiter", "TrackEvent", "TrackResult",
    "adapters", "normalise_status", "request_json",
]
