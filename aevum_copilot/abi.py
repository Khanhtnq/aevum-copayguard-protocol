"""Deterministic Solidity ABI calldata builders for tUSD8 & Aevum contracts."""

from __future__ import annotations

from aevum_copilot.keccak import function_selector, to_checksum_address


def _enc_uint256(val: int) -> str:
    if val < 0 or val >= (1 << 256):
        raise ValueError(f"uint256 out of bounds: {val}")
    return f"{val:064x}"


def _enc_bool(flag: bool) -> str:
    return _enc_uint256(1 if flag else 0)


def _enc_address(addr: str) -> str:
    checksummed = to_checksum_address(addr)
    return checksummed[2:].lower().rjust(64, "0")


def _enc_bytes32(hex32: str) -> str:
    cleaned = hex32.strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != 64:
        raise ValueError(f"Expected 32-byte hex string, got {hex32!r}")
    int(cleaned, 16)
    return cleaned


def encode_erc20_approve(spender: str, amount_units: int) -> str:
    sel = function_selector("approve(address,uint256)")
    return sel + _enc_address(spender) + _enc_uint256(amount_units)


def encode_register_payee(handle_hash: str, legal_name_hash: str, metadata_hash: str) -> str:
    sel = function_selector("registerPayee(bytes32,bytes32,bytes32)")
    return (
        sel
        + _enc_bytes32(handle_hash)
        + _enc_bytes32(legal_name_hash)
        + _enc_bytes32(metadata_hash)
    )


def encode_verify_payee_match(
    expected_wallet: str, expected_handle_hash: str, expected_legal_name_hash: str
) -> str:
    sel = function_selector("verifyPayeeMatch(address,bytes32,bytes32)")
    return (
        sel
        + _enc_address(expected_wallet)
        + _enc_bytes32(expected_handle_hash)
        + _enc_bytes32(expected_legal_name_hash)
    )


def encode_create_invoice(
    designated_payer: str,
    payee_handle_hash: str,
    principal_units: int,
    early_discount_bps: int,
    discount_deadline: int,
    due_at: int,
    invoice_ref_hash: str,
) -> str:
    sel = function_selector("createInvoice(address,bytes32,uint256,uint16,uint64,uint64,bytes32)")
    return (
        sel
        + _enc_address(designated_payer)
        + _enc_bytes32(payee_handle_hash)
        + _enc_uint256(principal_units)
        + _enc_uint256(early_discount_bps)
        + _enc_uint256(discount_deadline)
        + _enc_uint256(due_at)
        + _enc_bytes32(invoice_ref_hash)
    )


def encode_pay_verified_invoice(
    invoice_id: int,
    expected_payee_wallet: str,
    expected_handle_hash: str,
    expected_legal_name_hash: str,
    max_settlement_units: int,
) -> str:
    sel = function_selector("payVerifiedInvoice(uint256,address,bytes32,bytes32,uint256)")
    return (
        sel
        + _enc_uint256(invoice_id)
        + _enc_address(expected_payee_wallet)
        + _enc_bytes32(expected_handle_hash)
        + _enc_bytes32(expected_legal_name_hash)
        + _enc_uint256(max_settlement_units)
    )


def encode_create_milestone_escrow(
    payee: str,
    payee_handle_hash: str,
    expected_legal_name_hash: str,
    arbiter: str,
    tranche_amounts: list[int],
    expires_at: int,
    contract_ref_hash: str,
) -> str:
    if not tranche_amounts:
        raise ValueError("Milestone escrow requires at least one tranche")
    sel = function_selector(
        "createMilestoneEscrow(address,bytes32,bytes32,address,uint256[],uint64,bytes32)"
    )
    dynamic_offset = 7 * 32
    head = (
        _enc_address(payee)
        + _enc_bytes32(payee_handle_hash)
        + _enc_bytes32(expected_legal_name_hash)
        + _enc_address(arbiter)
        + _enc_uint256(dynamic_offset)
        + _enc_uint256(expires_at)
        + _enc_bytes32(contract_ref_hash)
    )
    tail = _enc_uint256(len(tranche_amounts)) + "".join(
        _enc_uint256(amt) for amt in tranche_amounts
    )
    return sel + head + tail


def encode_create_payroll_stream(
    recipient: str,
    payee_handle_hash: str,
    expected_legal_name_hash: str,
    deposit_units: int,
    start_at: int,
    cliff_at: int,
    end_at: int,
    cancelable: bool,
) -> str:
    sel = function_selector(
        "createPayrollStream(address,bytes32,bytes32,uint256,uint64,uint64,uint64,bool)"
    )
    return (
        sel
        + _enc_address(recipient)
        + _enc_bytes32(payee_handle_hash)
        + _enc_bytes32(expected_legal_name_hash)
        + _enc_uint256(deposit_units)
        + _enc_uint256(start_at)
        + _enc_uint256(cliff_at)
        + _enc_uint256(end_at)
        + _enc_bool(cancelable)
    )
