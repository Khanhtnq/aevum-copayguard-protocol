"""Confirmation-of-Payee (CoP) and EVM lookalike-address poisoning detector."""

from __future__ import annotations

from aevum_copilot.keccak import (
    keccak256_text,
    normalize_payee_handle,
    to_checksum_address,
)
from aevum_copilot.models import (
    GuardRiskLevel,
    PayeeGuardReport,
    PoisoningSimilarityMatch,
    TrustedPayeeContact,
)

DEFAULT_DEMO_DIRECTORY: tuple[TrustedPayeeContact, ...] = (
    TrustedPayeeContact(
        handle="acme.cloud.billing",
        legal_name="Acme Cloud Infrastructure Ltd",
        wallet="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    ),
    TrustedPayeeContact(
        handle="qday.logistics.apac",
        legal_name="QDay APAC Logistics Pte Ltd",
        wallet="0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    ),
    TrustedPayeeContact(
        handle="alice.eng.payroll",
        legal_name="Alice Nguyen",
        wallet="0x90F79bf6EB2c4f870365E785982E1f101E93b906",
    ),
)


def _common_prefix_len(a: str, b: str) -> int:
    count = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        count += 1
    return count


def _common_suffix_len(a: str, b: str) -> int:
    return _common_prefix_len(a[::-1], b[::-1])


def detect_lookalike_poisoning(
    candidate_wallet: str,
    trusted_contacts: list[TrustedPayeeContact] | tuple[TrustedPayeeContact, ...],
) -> list[PoisoningSimilarityMatch]:
    cand_hex = to_checksum_address(candidate_wallet)[2:].lower()
    matches: list[PoisoningSimilarityMatch] = []
    for contact in trusted_contacts:
        trusted_checksum = to_checksum_address(contact.wallet)
        trusted_hex = trusted_checksum[2:].lower()
        if cand_hex == trusted_hex:
            continue
        prefix = _common_prefix_len(cand_hex, trusted_hex)
        suffix = _common_suffix_len(cand_hex, trusted_hex)
        if prefix >= 3 and suffix >= 3 and (prefix + suffix) >= 7:
            matches.append(
                PoisoningSimilarityMatch(
                    trusted_handle=normalize_payee_handle(contact.handle),
                    trusted_wallet=trusted_checksum,
                    candidate_wallet=to_checksum_address(candidate_wallet),
                    shared_prefix_hex_chars=prefix,
                    shared_suffix_hex_chars=suffix,
                    reason=(
                        f"Shares {prefix} leading and {suffix} trailing hex chars with "
                        f"trusted payee '{contact.handle}' ({trusted_checksum}) while "
                        "middle bytes differ (classic vanity address-poisoning signature)."
                    ),
                )
            )
    return matches


def _check_checksum_match(raw_wallet: str, checksummed: str) -> bool:
    body = raw_wallet.strip()[2:]
    if body.islower() or body.isupper():
        return True
    return raw_wallet.strip() == checksummed


def evaluate_payee_guard(
    *,
    candidate_wallet: str,
    payee_handle: str,
    legal_name: str,
    trusted_contacts: (
        list[TrustedPayeeContact] | tuple[TrustedPayeeContact, ...]
    ) = DEFAULT_DEMO_DIRECTORY,
    onchain_verified: bool | None = None,
) -> PayeeGuardReport:
    normalized_handle = normalize_payee_handle(payee_handle)
    clean_legal = legal_name.strip() or normalized_handle
    checksummed = to_checksum_address(candidate_wallet)
    checksum_ok = _check_checksum_match(candidate_wallet, checksummed)

    handle_hash = keccak256_text(normalized_handle)
    legal_hash = keccak256_text(clean_legal)

    warnings: list[str] = []
    if not checksum_ok:
        warnings.append("Candidate address failed mixed-case EIP-55 checksum validation.")

    poisoning = detect_lookalike_poisoning(checksummed, trusted_contacts)
    handle_record = next(
        (c for c in trusted_contacts if normalize_payee_handle(c.handle) == normalized_handle),
        None,
    )

    risk_level, blocked = _classify_risk(
        checksummed=checksummed,
        clean_legal=clean_legal,
        handle_record=handle_record,
        poisoning=poisoning,
        onchain_verified=onchain_verified,
        warnings=warnings,
    )

    return PayeeGuardReport(
        candidate_wallet=checksummed,
        normalized_handle=normalized_handle,
        handle_hash=handle_hash,
        legal_name=clean_legal,
        legal_name_hash=legal_hash,
        checksum_valid=checksum_ok,
        onchain_verified=onchain_verified,
        risk_level=risk_level,
        blocked=blocked,
        warnings=warnings,
        poisoning_matches=poisoning,
    )


def _classify_risk(
    *,
    checksummed: str,
    clean_legal: str,
    handle_record: TrustedPayeeContact | None,
    poisoning: list[PoisoningSimilarityMatch],
    onchain_verified: bool | None,
    warnings: list[str],
) -> tuple[GuardRiskLevel, bool]:
    if poisoning:
        warnings.extend(m.reason for m in poisoning)
        return GuardRiskLevel.CRITICAL_POISONING, True

    if onchain_verified is False:
        warnings.append("On-chain AevumPayeeRegistry rejected handle/wallet/legal-name match.")
        return GuardRiskLevel.CRITICAL_MISMATCH, True

    if handle_record is not None:
        expected_wallet = to_checksum_address(handle_record.wallet)
        if expected_wallet != checksummed:
            warnings.append(
                f"Handle '{handle_record.handle}' is bound to {expected_wallet}, not {checksummed}."
            )
            return GuardRiskLevel.CRITICAL_MISMATCH, True
        if handle_record.legal_name.strip().lower() != clean_legal.lower():
            warnings.append(
                f"Legal entity mismatch for '{handle_record.handle}': expected "
                f"'{handle_record.legal_name}', got '{clean_legal}'."
            )
            return GuardRiskLevel.CRITICAL_MISMATCH, True
        return GuardRiskLevel.SAFE, False

    if onchain_verified is True:
        return GuardRiskLevel.SAFE, False

    warnings.append(
        "Payee handle is not yet verified in local directory or on-chain AevumPayeeRegistry."
    )
    return GuardRiskLevel.WARN_UNVERIFIED, False
