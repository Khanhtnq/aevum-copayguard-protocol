"""Typed 6-decimal tUSD8 models and precision-safe unit converters."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, Field

from aevum_copilot import TUSD8_DECIMALS, TUSD8_UNIT

_QUANTUM = Decimal("0.000001")


class PaymentFlowType(StrEnum):
    INVOICE = "invoice"
    MILESTONE_ESCROW = "milestone_escrow"
    PAYROLL_STREAM = "payroll_stream"


class GuardRiskLevel(StrEnum):
    SAFE = "SAFE"
    WARN_UNVERIFIED = "WARN_UNVERIFIED"
    CRITICAL_POISONING = "CRITICAL_POISONING"
    CRITICAL_MISMATCH = "CRITICAL_MISMATCH"


def parse_tusd8_units(raw_amount: str | int | float | Decimal) -> int:
    """Parse a human tUSD8 decimal string (e.g. '1,250.50') into 6-decimal atomic units."""
    cleaned = str(raw_amount).strip().replace(",", "").replace("_", "")
    if cleaned.lower().endswith("tusd8"):
        cleaned = cleaned[:-5].strip()
    if not cleaned:
        raise ValueError("tUSD8 amount cannot be empty")
    try:
        dec = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid tUSD8 decimal amount: {raw_amount!r}") from exc
    if dec <= 0:
        raise ValueError("tUSD8 amount must be strictly positive")
    exponent = dec.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > TUSD8_DECIMALS:
        raise ValueError(f"tUSD8 supports at most {TUSD8_DECIMALS} decimals; got {raw_amount!r}")
    return int((dec * Decimal(TUSD8_UNIT)).to_integral_exact())


def format_tusd8_units(atomic_units: int) -> str:
    if atomic_units < 0:
        raise ValueError("Atomic tUSD8 units cannot be negative")
    value = (Decimal(atomic_units) / Decimal(TUSD8_UNIT)).quantize(_QUANTUM)
    return f"{value:.6f}"


class TrustedPayeeContact(BaseModel):
    handle: str
    legal_name: str
    wallet: str


class PoisoningSimilarityMatch(BaseModel):
    trusted_handle: str
    trusted_wallet: str
    candidate_wallet: str
    shared_prefix_hex_chars: int
    shared_suffix_hex_chars: int
    reason: str


class PayeeGuardReport(BaseModel):
    candidate_wallet: str
    normalized_handle: str
    handle_hash: str
    legal_name: str
    legal_name_hash: str
    checksum_valid: bool
    onchain_verified: bool | None = None
    risk_level: GuardRiskLevel
    blocked: bool
    warnings: list[str] = Field(default_factory=list)
    poisoning_matches: list[PoisoningSimilarityMatch] = Field(default_factory=list)


class PreparedEvmCall(BaseModel):
    step: int
    description: str
    to: str
    function_signature: str
    calldata: str
    value_wei: str = "0x0"


class CopilotPaymentPlan(BaseModel):
    flow_type: PaymentFlowType
    parser_mode: str
    summary: str
    payee_handle: str
    payee_handle_hash: str
    payee_legal_name: str
    payee_legal_name_hash: str
    payee_wallet: str
    token_address: str
    token_symbol: str = "tUSD8"
    token_decimals: int = TUSD8_DECIMALS
    total_amount_units: int
    total_amount_formatted: str
    reference_id: str
    reference_hash: str
    early_discount_bps: int = 0
    discount_window_hours: int = 0
    due_in_days: int = 14
    tranche_amounts_units: list[int] = Field(default_factory=list)
    arbiter_wallet: str | None = None
    stream_duration_days: int = 0
    stream_cliff_days: int = 0
    guard_report: PayeeGuardReport
    transactions: list[PreparedEvmCall] = Field(default_factory=list)


class AevumNetworkSnapshot(BaseModel):
    rpc_url: str
    reachable: bool
    chain_id: int
    expected_chain_id: int
    latest_block: int
    gas_price_wei: int
    tusd8_address: str
    tusd8_symbol: str
    tusd8_decimals: int
    tusd8_total_supply_units: int
    tusd8_total_supply_formatted: str
    error: str | None = None


class AccountBalanceSnapshot(BaseModel):
    address: str
    tqday_wei: int
    tqday_formatted: str
    tusd8_units: int
    tusd8_formatted: str
