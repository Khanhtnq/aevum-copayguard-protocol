"""Native AI Stablecoin Payment Copilot ($0 local Ollama + deterministic fallback)."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from aevum_copilot import AEVUM_TUSD8_ADDRESS
from aevum_copilot.abi import (
    encode_create_milestone_escrow,
    encode_create_payroll_stream,
    encode_erc20_approve,
    encode_pay_verified_invoice,
)
from aevum_copilot.guard import DEFAULT_DEMO_DIRECTORY, evaluate_payee_guard
from aevum_copilot.keccak import keccak256_text, to_checksum_address
from aevum_copilot.models import (
    CopilotPaymentPlan,
    PaymentFlowType,
    PreparedEvmCall,
    TrustedPayeeContact,
    format_tusd8_units,
    parse_tusd8_units,
)

DEFAULT_VAULT_PLACEHOLDER = "0x1111111111111111111111111111111111114405"
DEFAULT_ARBITER_PLACEHOLDER = "0x2222222222222222222222222222222222224405"

_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_AMOUNT_RE = re.compile(r"([0-9][0-9,]*(?:\.[0-9]{1,6})?)\s*tUSD8", re.IGNORECASE)
_HANDLE_RE = re.compile(r"\b([a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+){1,4})\b")
_DISCOUNT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*(?:early\s*)?discount", re.IGNORECASE)
_REF_RE = re.compile(r"\b((?:INV|MSA|PAY|ESC)-[A-Z0-9-]+)\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*[- ]?day", re.IGNORECASE)
_CLIFF_RE = re.compile(r"(\d+)\s*[- ]?day\s+cliff", re.IGNORECASE)
_TRANCHE_RE = re.compile(
    r"tranches?\s*(?::\s*)?(?:[\[\(]([^\[\]\(\)]+)[\]\)]|([0-9.,\s/]+))",
    re.IGNORECASE,
)
_TRANCHE_NUM_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d{1,6})?\b|\b\d+(?:\.\d{1,6})?\b")

_OLLAMA_SYSTEM_PROMPT = (
    "You are Aevum CopayGuard AI, a precision financial parser for 6-decimal tUSD8 stablecoin "
    "settlements on QDay Aevum Testnet (Chain ID 44005). Extract JSON with exact keys: "
    "flow_type ('invoice' | 'milestone_escrow' | 'payroll_stream'), amount_tusd8 (string decimal), "
    "payee_handle (dot-separated handle), payee_legal_name (string), payee_wallet (0x 40-hex), "
    "reference_id (string), early_discount_percent (number, default 0), "
    "due_in_days (int, default 14), tranches_tusd8 (list of string decimals, optional), "
    "stream_duration_days (int, default 30), stream_cliff_days (int, default 0). "
    "Output strictly valid JSON only."
)


def _detect_flow_type(instruction: str) -> PaymentFlowType:
    lower = instruction.lower()
    if any(word in lower for word in ("stream", "payroll", "vesting", "salary")):
        return PaymentFlowType.PAYROLL_STREAM
    if any(word in lower for word in ("milestone", "escrow", "tranche")):
        return PaymentFlowType.MILESTONE_ESCROW
    return PaymentFlowType.INVOICE


def _match_contact(
    instruction: str, contacts: tuple[TrustedPayeeContact, ...]
) -> TrustedPayeeContact:
    lower = instruction.lower()
    for contact in contacts:
        if contact.handle.lower() in lower or contact.legal_name.lower() in lower:
            return contact
    return contacts[0]


def _extract_balanced_parens(text: str) -> list[tuple[int, int, str]]:
    """Return top-level balanced parenthetical spans: (start_idx, end_idx, inner_content)."""
    results: list[tuple[int, int, str]] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    inner = text[start + 1 : i].strip()
                    results.append((start, i + 1, inner))
                    start = -1
    return results


def _is_valid_legal_name_candidate(cand: str) -> bool:
    if len(cand) < 3 or len(cand) > 80:
        return False
    if not any(ch.isalpha() for ch in cand):
        return False
    lower = cand.lower()
    if "discount" in lower or "tranche" in lower or lower == "tusd8":
        return False
    cleaned = "".join(ch for ch in cand if ch.isalnum())
    return any(ch.isalpha() for ch in cleaned)


def _extract_legal_name(instruction: str, handle: str, default_legal_name: str) -> str:
    spans = _extract_balanced_parens(instruction)
    handle_pos = instruction.lower().find(handle.lower()) if handle else -1
    if handle_pos != -1:
        after = [
            inner
            for start, _end, inner in spans
            if start >= handle_pos and _is_valid_legal_name_candidate(inner)
        ]
        if after:
            return after[0]
        before = [
            inner
            for _start, end, inner in spans
            if end <= handle_pos and _is_valid_legal_name_candidate(inner)
        ]
        if before:
            return before[-1]
    valid_any = [inner for _start, _end, inner in spans if _is_valid_legal_name_candidate(inner)]
    if valid_any:
        return valid_any[0]
    return default_legal_name


def _extract_deterministic_fields(
    instruction: str,
    contacts: tuple[TrustedPayeeContact, ...],
) -> dict[str, Any]:
    flow = _detect_flow_type(instruction)
    matched_contact = _match_contact(instruction, contacts)

    wallets = _ADDRESS_RE.findall(instruction)
    wallet = wallets[0] if wallets else matched_contact.wallet

    handles = [h for h in _HANDLE_RE.findall(instruction) if not h.replace(".", "").isdigit()]
    handle = handles[0] if handles else matched_contact.handle

    legal_name = _extract_legal_name(instruction, handle, matched_contact.legal_name)

    amt_match = _AMOUNT_RE.search(instruction)
    amount_str = amt_match.group(1) if amt_match else "100.00"

    disc_match = _DISCOUNT_RE.search(instruction)
    discount_pct = float(disc_match.group(1)) if disc_match else 0.0

    ref_match = _REF_RE.search(instruction)
    default_prefix = {
        PaymentFlowType.INVOICE: "INV-AEVUM-001",
        PaymentFlowType.MILESTONE_ESCROW: "MSA-AEVUM-001",
        PaymentFlowType.PAYROLL_STREAM: "PAY-AEVUM-001",
    }[flow]
    ref_id = ref_match.group(1).upper() if ref_match else default_prefix

    cliff_match = _CLIFF_RE.search(instruction)
    cliff_days = int(cliff_match.group(1)) if cliff_match else 0
    day_matches = [int(d) for d in _DAYS_RE.findall(instruction)]
    duration_days = max(day_matches) if day_matches else 30

    tranches = _parse_tranche_strings(instruction)
    return {
        "flow_type": flow.value,
        "amount_tusd8": amount_str,
        "payee_handle": handle,
        "payee_legal_name": legal_name,
        "payee_wallet": wallet,
        "reference_id": ref_id,
        "early_discount_percent": discount_pct,
        "due_in_days": duration_days if day_matches else 14,
        "tranches_tusd8": tranches,
        "stream_duration_days": duration_days,
        "stream_cliff_days": min(cliff_days, duration_days),
    }


def _parse_tranche_strings(instruction: str) -> list[str]:
    # Look for bracketed or parenthesized tranche list e.g. [100, 200] or (100, 200)
    bracket_match = re.search(
        r"tranches?\s*(?::\s*)?[\[\(]\s*([^\[\]\(\)]+)\s*[\]\)]",
        instruction,
        re.IGNORECASE,
    )
    if bracket_match:
        target = bracket_match.group(1)
    else:
        colon_match = re.search(
            r"tranches?\s*(?::\s*|\s+)([0-9][0-9.,\s/]+)",
            instruction,
            re.IGNORECASE,
        )
        if not colon_match:
            return []
        target = colon_match.group(1)
    raw_nums = [n.replace(",", "") for n in _TRANCHE_NUM_RE.findall(target)]
    return raw_nums[:10]


def _query_ollama_json(
    instruction: str,
    *,
    ollama_url: str,
    model: str,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": _OLLAMA_SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ],
        "options": {"temperature": 0},
    }
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _build_transactions(
    *,
    flow: PaymentFlowType,
    vault_address: str,
    token_address: str,
    payee_wallet: str,
    handle_hash: str,
    legal_hash: str,
    total_units: int,
    ref_hash: str,
    tranche_units: list[int],
    arbiter_wallet: str,
    due_in_days: int,
    stream_duration_days: int,
    stream_cliff_days: int,
    now_ts: int,
) -> list[PreparedEvmCall]:
    approve_desc = (
        f"Approve {format_tusd8_units(total_units)} tUSD8 (6 decimals) to AevumInvoiceVault"
    )
    approve_call = PreparedEvmCall(
        step=1,
        description=approve_desc,
        to=to_checksum_address(token_address),
        function_signature="approve(address,uint256)",
        calldata=encode_erc20_approve(vault_address, total_units),
    )

    if flow == PaymentFlowType.MILESTONE_ESCROW:
        escrow_sig = (
            "createMilestoneEscrow(address,bytes32,bytes32,address,uint256[],uint64,bytes32)"
        )
        escrow_desc = f"Lock {len(tranche_units)} verified milestone tranches in AevumInvoiceVault"
        settlement_call = PreparedEvmCall(
            step=2,
            description=escrow_desc,
            to=to_checksum_address(vault_address),
            function_signature=escrow_sig,
            calldata=encode_create_milestone_escrow(
                payee_wallet,
                handle_hash,
                legal_hash,
                arbiter_wallet,
                tranche_units,
                now_ts + max(due_in_days, 1) * 86_400,
                ref_hash,
            ),
        )
    elif flow == PaymentFlowType.PAYROLL_STREAM:
        start_at = now_ts + 60
        cliff_at = start_at + max(stream_cliff_days, 0) * 86_400
        end_at = start_at + max(stream_duration_days, 1) * 86_400
        stream_sig = (
            "createPayrollStream(address,bytes32,bytes32,uint256,uint64,uint64,uint64,bool)"
        )
        stream_desc = (
            f"Open {stream_duration_days}-day linear tUSD8 payroll stream in AevumInvoiceVault"
        )
        settlement_call = PreparedEvmCall(
            step=2,
            description=stream_desc,
            to=to_checksum_address(vault_address),
            function_signature=stream_sig,
            calldata=encode_create_payroll_stream(
                payee_wallet,
                handle_hash,
                legal_hash,
                total_units,
                start_at,
                cliff_at,
                end_at,
                True,
            ),
        )
    else:
        settlement_call = PreparedEvmCall(
            step=2,
            description="Settle invoice #1 atomically with on-chain Confirmation-of-Payee guard",
            to=to_checksum_address(vault_address),
            function_signature="payVerifiedInvoice(uint256,address,bytes32,bytes32,uint256)",
            calldata=encode_pay_verified_invoice(
                1,
                payee_wallet,
                handle_hash,
                legal_hash,
                total_units,
            ),
        )
    return [approve_call, settlement_call]


def build_copilot_payment_plan(
    instruction: str,
    *,
    vault_address: str = DEFAULT_VAULT_PLACEHOLDER,
    arbiter_address: str = DEFAULT_ARBITER_PLACEHOLDER,
    token_address: str = AEVUM_TUSD8_ADDRESS,
    trusted_contacts: tuple[TrustedPayeeContact, ...] = DEFAULT_DEMO_DIRECTORY,
    use_ollama: bool = False,
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "qwen2.5:7b-instruct",
    now_timestamp: int | None = None,
) -> CopilotPaymentPlan:
    fallback = _extract_deterministic_fields(instruction, trusted_contacts)
    llm_fields = (
        _query_ollama_json(
            instruction,
            ollama_url=ollama_url,
            model=ollama_model,
            timeout_seconds=20.0,
        )
        if use_ollama
        else None
    )
    merged, parser_mode = _merge_extracted_fields(fallback, llm_fields, ollama_model)

    flow = PaymentFlowType(merged["flow_type"])
    tranche_units = [parse_tusd8_units(x) for x in merged["tranches_tusd8"]]
    if flow == PaymentFlowType.MILESTONE_ESCROW and not tranche_units:
        half = parse_tusd8_units(merged["amount_tusd8"]) // 2
        rem = parse_tusd8_units(merged["amount_tusd8"]) - half
        tranche_units = [half, rem] if half > 0 else [rem]

    total_units = (
        sum(tranche_units)
        if flow == PaymentFlowType.MILESTONE_ESCROW
        else parse_tusd8_units(merged["amount_tusd8"])
    )
    discount_bps = min(max(int(round(float(merged["early_discount_percent"]) * 100)), 0), 5000)

    guard = evaluate_payee_guard(
        candidate_wallet=merged["payee_wallet"],
        payee_handle=merged["payee_handle"],
        legal_name=merged["payee_legal_name"],
        trusted_contacts=trusted_contacts,
    )

    ref_id = str(merged["reference_id"]).strip() or "INV-AEVUM-001"
    ref_hash = keccak256_text(ref_id)
    now_ts = int(time.time()) if now_timestamp is None else now_timestamp

    txs = (
        []
        if guard.blocked
        else _build_transactions(
            flow=flow,
            vault_address=vault_address,
            token_address=token_address,
            payee_wallet=guard.candidate_wallet,
            handle_hash=guard.handle_hash,
            legal_hash=guard.legal_name_hash,
            total_units=total_units,
            ref_hash=ref_hash,
            tranche_units=tranche_units,
            arbiter_wallet=arbiter_address,
            due_in_days=int(merged["due_in_days"]),
            stream_duration_days=int(merged["stream_duration_days"]),
            stream_cliff_days=int(merged["stream_cliff_days"]),
            now_ts=now_ts,
        )
    )

    status_tag = "BLOCKED BY COPAYGUARD" if guard.blocked else "READY FOR SIGNING"
    summary = (
        f"[{status_tag}] {flow.value} for {format_tusd8_units(total_units)} tUSD8 "
        f"to {guard.normalized_handle} ({guard.candidate_wallet})"
    )

    return CopilotPaymentPlan(
        flow_type=flow,
        parser_mode=parser_mode,
        summary=summary,
        payee_handle=guard.normalized_handle,
        payee_handle_hash=guard.handle_hash,
        payee_legal_name=guard.legal_name,
        payee_legal_name_hash=guard.legal_name_hash,
        payee_wallet=guard.candidate_wallet,
        token_address=to_checksum_address(token_address),
        total_amount_units=total_units,
        total_amount_formatted=format_tusd8_units(total_units),
        reference_id=ref_id,
        reference_hash=ref_hash,
        early_discount_bps=discount_bps,
        discount_window_hours=48 if discount_bps > 0 else 0,
        due_in_days=int(merged["due_in_days"]),
        tranche_amounts_units=tranche_units,
        arbiter_wallet=to_checksum_address(arbiter_address)
        if flow == PaymentFlowType.MILESTONE_ESCROW
        else None,
        stream_duration_days=int(merged["stream_duration_days"])
        if flow == PaymentFlowType.PAYROLL_STREAM
        else 0,
        stream_cliff_days=int(merged["stream_cliff_days"])
        if flow == PaymentFlowType.PAYROLL_STREAM
        else 0,
        guard_report=guard,
        transactions=txs,
    )


def _merge_extracted_fields(
    fallback: dict[str, Any],
    llm: dict[str, Any] | None,
    ollama_model: str,
) -> tuple[dict[str, Any], str]:
    if not llm:
        return fallback, "deterministic-v1"
    merged = dict(fallback)
    if llm.get("flow_type") in {"invoice", "milestone_escrow", "payroll_stream"}:
        merged["flow_type"] = llm["flow_type"]
    for key in ("amount_tusd8", "payee_handle", "payee_legal_name", "reference_id"):
        val = llm.get(key)
        if isinstance(val, (str, int, float)) and str(val).strip():
            merged[key] = str(val).strip()
    wallet = str(llm.get("payee_wallet") or "").strip()
    if _ADDRESS_RE.fullmatch(wallet):
        merged["payee_wallet"] = wallet
    num_keys = (
        "early_discount_percent",
        "due_in_days",
        "stream_duration_days",
        "stream_cliff_days",
    )
    for num_key in num_keys:
        if isinstance(llm.get(num_key), (int, float)):
            merged[num_key] = llm[num_key]
    raw_tranches = llm.get("tranches_tusd8")
    if isinstance(raw_tranches, list) and raw_tranches:
        merged["tranches_tusd8"] = [str(item) for item in raw_tranches[:10]]
    return merged, f"ollama:{ollama_model}"
