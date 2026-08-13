"""RTL verification automation: analyze RTL, generate TB, simulate, report."""

from .pipeline import VerificationResult, run_verification
from .rtl_profile import InputKind, RtlProfile, parse_rtl_profile
from .generators.comb_assert_tb import generate as generate_combinational_assert_tb

__all__ = [
    "VerificationResult",
    "run_verification",
    "RtlProfile",
    "parse_rtl_profile",
    "InputKind",
    "generate_combinational_assert_tb",
]
