"""RTL verification automation: analyze RTL, generate TB, simulate, report."""

from .pipeline import VerificationResult, run_verification

__all__ = ["VerificationResult", "run_verification"]
