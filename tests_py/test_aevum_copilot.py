"""End-to-end test suite for Aevum CopayGuard & Invoice Escrow Python stack."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from aevum_copilot import (
    AEVUM_CHAIN_ID,
    AEVUM_CHAIN_ID_HEX,
    AEVUM_TUSD8_ADDRESS,
    AEVUM_TUSD8_FAUCET,
    TUSD8_DECIMALS,
)
from aevum_copilot.abi import (
    encode_create_invoice,
    encode_create_milestone_escrow,
    encode_create_payroll_stream,
    encode_erc20_approve,
    encode_pay_verified_invoice,
    encode_register_payee,
    encode_verify_payee_match,
)
from aevum_copilot.cli import main as cli_main
from aevum_copilot.copilot import (
    DEFAULT_ARBITER_PLACEHOLDER,
    DEFAULT_VAULT_PLACEHOLDER,
    build_copilot_payment_plan,
)
from aevum_copilot.guard import (
    DEFAULT_DEMO_DIRECTORY,
    detect_lookalike_poisoning,
    evaluate_payee_guard,
)
from aevum_copilot.keccak import (
    function_selector,
    keccak256,
    keccak256_text,
    normalize_payee_handle,
    to_checksum_address,
)
from aevum_copilot.models import (
    GuardRiskLevel,
    PaymentFlowType,
    format_tusd8_units,
    parse_tusd8_units,
)
from aevum_copilot.rpc import AevumRpcClient
from aevum_copilot.server import AevumAppState, create_http_server


def _find_cast_binary() -> str:
    found = shutil.which("cast")
    if found:
        return found
    home_cast = Path.home() / ".foundry" / "bin" / "cast.exe"
    if home_cast.exists():
        return str(home_cast)
    posix_cast = Path.home() / ".foundry" / "bin" / "cast"
    if posix_cast.exists():
        return str(posix_cast)
    raise FileNotFoundError("Foundry 'cast' binary is required for ABI parity verification")


def _run_cast(args: list[str]) -> str:
    cast_bin = _find_cast_binary()
    proc = subprocess.run(
        [cast_bin, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.stdout.strip()


def test_keccak256_standard_vectors_and_eip55() -> None:
    empty_hex = keccak256(b"").hex()
    assert empty_hex == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert function_selector("transfer(address,uint256)") == "0xa9059cbb"
    assert function_selector("approve(address,uint256)") == "0x095ea7b3"
    long_msg = b"a" * 200
    assert "0x" + keccak256(long_msg).hex() == _run_cast(["keccak", long_msg.decode("ascii")])
    assert (
        to_checksum_address("0x456569838f1d43009a91eba4263147afd7233483")
        == AEVUM_TUSD8_ADDRESS
    )
    assert normalize_payee_handle("  @Acme.Cloud.Billing ") == "acme.cloud.billing"


def test_abi_encoders_match_foundry_cast_byte_for_byte() -> None:
    handle_hash = keccak256_text("acme.cloud.billing")
    legal_hash = keccak256_text("Acme Cloud Infrastructure Ltd")
    meta_hash = keccak256_text("ipfs://acme-v1")
    payee = DEFAULT_DEMO_DIRECTORY[0].wallet

    assert encode_erc20_approve(DEFAULT_VAULT_PLACEHOLDER, 1_250_500_000) == _run_cast(
        ["calldata", "approve(address,uint256)", DEFAULT_VAULT_PLACEHOLDER, "1250500000"]
    )
    assert encode_register_payee(handle_hash, legal_hash, meta_hash) == _run_cast(
        ["calldata", "registerPayee(bytes32,bytes32,bytes32)", handle_hash, legal_hash, meta_hash]
    )
    assert encode_verify_payee_match(payee, handle_hash, legal_hash) == _run_cast(
        ["calldata", "verifyPayeeMatch(address,bytes32,bytes32)", payee, handle_hash, legal_hash]
    )
    assert encode_create_invoice(
        payee, handle_hash, 2_500_000_000, 200, 1_765_086_400, 1_766_209_600, meta_hash
    ) == _run_cast(
        [
            "calldata",
            "createInvoice(address,bytes32,uint256,uint16,uint64,uint64,bytes32)",
            payee,
            handle_hash,
            "2500000000",
            "200",
            "1765086400",
            "1766209600",
            meta_hash,
        ]
    )
    assert encode_pay_verified_invoice(1, payee, handle_hash, legal_hash, 1_250_500_000) == (
        _run_cast(
            [
                "calldata",
                "payVerifiedInvoice(uint256,address,bytes32,bytes32,uint256)",
                "1",
                payee,
                handle_hash,
                legal_hash,
                "1250500000",
            ]
        )
    )


def test_dynamic_escrow_and_stream_abi_match_foundry_cast() -> None:
    handle_hash = keccak256_text("qday.logistics.apac")
    legal_hash = keccak256_text("QDay APAC Logistics Pte Ltd")
    ref_hash = keccak256_text("MSA-2026-310")
    payee = DEFAULT_DEMO_DIRECTORY[1].wallet

    escrow_calldata = encode_create_milestone_escrow(
        payee,
        handle_hash,
        legal_hash,
        DEFAULT_ARBITER_PLACEHOLDER,
        [1_500_000_000, 2_500_000_000],
        1_766_814_400,
        ref_hash,
    )
    expected_escrow = _run_cast(
        [
            "calldata",
            "createMilestoneEscrow(address,bytes32,bytes32,address,uint256[],uint64,bytes32)",
            payee,
            handle_hash,
            legal_hash,
            DEFAULT_ARBITER_PLACEHOLDER,
            "[1500000000,2500000000]",
            "1766814400",
            ref_hash,
        ]
    )
    assert escrow_calldata == expected_escrow

    stream_calldata = encode_create_payroll_stream(
        payee,
        handle_hash,
        legal_hash,
        6_000_000_000,
        1_765_000_060,
        1_765_604_860,
        1_767_592_060,
        True,
    )
    expected_stream = _run_cast(
        [
            "calldata",
            "createPayrollStream(address,bytes32,bytes32,uint256,uint64,uint64,uint64,bool)",
            payee,
            handle_hash,
            legal_hash,
            "6000000000",
            "1765000060",
            "1765604860",
            "1767592060",
            "true",
        ]
    )
    assert stream_calldata == expected_stream


def test_tusd8_six_decimal_unit_math_and_validation() -> None:
    assert TUSD8_DECIMALS == 6
    assert parse_tusd8_units("1") == 1_000_000
    assert parse_tusd8_units("1,250.50 tUSD8") == 1_250_500_000
    assert parse_tusd8_units("0.000001") == 1
    assert format_tusd8_units(1_250_500_000) == "1250.500000"
    assert format_tusd8_units(0) == "0.000000"

    for invalid in ("", "0", "-5", "0.0000001", "not-a-number"):
        with pytest.raises(ValueError):
            parse_tusd8_units(invalid)
    with pytest.raises(ValueError):
        format_tusd8_units(-1)


def test_payee_guard_detects_poisoning_mismatch_and_checksum() -> None:
    safe_report = evaluate_payee_guard(
        candidate_wallet="0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        payee_handle="acme.cloud.billing",
        legal_name="Acme Cloud Infrastructure Ltd",
    )
    assert safe_report.risk_level == GuardRiskLevel.SAFE
    assert safe_report.blocked is False
    assert safe_report.checksum_valid is True

    poison_wallet = "0x7099790000000000000000000000000000dc79C8"
    matches = detect_lookalike_poisoning(poison_wallet, DEFAULT_DEMO_DIRECTORY)
    assert len(matches) == 1
    assert matches[0].shared_prefix_hex_chars >= 6
    assert matches[0].shared_suffix_hex_chars >= 6

    poison_report = evaluate_payee_guard(
        candidate_wallet=poison_wallet,
        payee_handle="acme.cloud.billing",
        legal_name="Acme Cloud Infrastructure Ltd",
    )
    assert poison_report.risk_level == GuardRiskLevel.CRITICAL_POISONING
    assert poison_report.blocked is True

    wrong_wallet = evaluate_payee_guard(
        candidate_wallet="0x1234567890123456789012345678901234567890",
        payee_handle="acme.cloud.billing",
        legal_name="Acme Cloud Infrastructure Ltd",
    )
    assert wrong_wallet.risk_level == GuardRiskLevel.CRITICAL_MISMATCH
    assert wrong_wallet.blocked is True

    bad_checksum = evaluate_payee_guard(
        candidate_wallet="0x1234567890abcdef1234567890ABCDEF12345678",
        payee_handle="new.supplier.io",
        legal_name="New Supplier Ltd",
        onchain_verified=True,
    )
    assert bad_checksum.checksum_valid is False
    assert bad_checksum.risk_level == GuardRiskLevel.SAFE


def test_copilot_payment_plans_across_all_four_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    inv_plan = build_copilot_payment_plan(
        "Pay invoice INV-2026-884 for 1,250.50 tUSD8 to acme.cloud.billing "
        "(Acme Cloud Infrastructure Ltd) at 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 "
        "with 2% early discount, due in 14 days.",
        now_timestamp=1_765_000_000,
    )
    assert inv_plan.flow_type == PaymentFlowType.INVOICE
    assert inv_plan.total_amount_units == 1_250_500_000
    assert inv_plan.early_discount_bps == 200
    assert len(inv_plan.transactions) == 2

    esc_plan = build_copilot_payment_plan(
        "Create milestone escrow MSA-2026-310 for 4,000 tUSD8 to qday.logistics.apac "
        "(QDay APAC Logistics Pte Ltd) at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC "
        "with tranches: 1500, 2500 over 21 days.",
        now_timestamp=1_765_000_000,
    )
    assert esc_plan.flow_type == PaymentFlowType.MILESTONE_ESCROW
    assert esc_plan.tranche_amounts_units == [1_500_000_000, 2_500_000_000]
    assert esc_plan.total_amount_units == 4_000_000_000
    assert len(esc_plan.transactions) == 2

    stream_plan = build_copilot_payment_plan(
        "Open payroll stream PAY-2026-09 for 6,000.00 tUSD8 to alice.eng.payroll "
        "(Alice Nguyen) at 0x90F79bf6EB2c4f870365E785982E1f101E93b906 over 30 days "
        "with 7-day cliff.",
        now_timestamp=1_765_000_000,
    )
    assert stream_plan.flow_type == PaymentFlowType.PAYROLL_STREAM
    assert stream_plan.stream_duration_days == 30
    assert stream_plan.stream_cliff_days == 7
    assert len(stream_plan.transactions) == 2

    blocked_plan = build_copilot_payment_plan(
        "Pay invoice INV-2026-999 for 5,000 tUSD8 to acme.cloud.billing "
        "(Acme Cloud Infrastructure Ltd) at 0x7099790000000000000000000000000000dc79C8.",
        now_timestamp=1_765_000_000,
    )
    assert blocked_plan.guard_report.blocked is True
    assert blocked_plan.guard_report.risk_level == GuardRiskLevel.CRITICAL_POISONING
    assert blocked_plan.transactions == []

    paren_tranche_plan = build_copilot_payment_plan(
        "Create milestone escrow MSA-2026-401 for 1,000 tUSD8 to qday.logistics.apac "
        "at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC with tranches (400, 600) over 14 days.",
        now_timestamp=1_765_000_000,
    )
    assert paren_tranche_plan.guard_report.risk_level == GuardRiskLevel.SAFE
    assert paren_tranche_plan.guard_report.blocked is False
    assert paren_tranche_plan.payee_legal_name == "QDay APAC Logistics Pte Ltd"

    def _fake_ollama(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "flow_type": "invoice",
            "amount_tusd8": "777.25",
            "payee_handle": "acme.cloud.billing",
            "payee_legal_name": "Acme Cloud Infrastructure Ltd",
            "payee_wallet": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "reference_id": "INV-LLM-777",
            "early_discount_percent": 1.5,
        }

    monkeypatch.setattr("aevum_copilot.copilot._query_ollama_json", _fake_ollama)
    llm_plan = build_copilot_payment_plan("Settle Acme cloud bill", use_ollama=True)
    assert llm_plan.parser_mode.startswith("ollama:")
    assert llm_plan.total_amount_units == 777_250_000
    assert llm_plan.early_discount_bps == 150


def test_live_aevum_testnet_rpc_and_tusd8_telemetry() -> None:
    rpc = AevumRpcClient()
    snap = rpc.fetch_network_snapshot()
    assert snap.reachable is True, f"Aevum RPC unreachable: {snap.error}"
    assert snap.chain_id == AEVUM_CHAIN_ID == 44005
    assert snap.latest_block > 0
    assert snap.tusd8_address == AEVUM_TUSD8_ADDRESS
    assert snap.tusd8_symbol == "tUSD8"
    assert snap.tusd8_decimals == 6
    assert snap.tusd8_total_supply_units > 0

    faucet_bal = rpc.fetch_account_balances(AEVUM_TUSD8_FAUCET)
    assert faucet_bal.address == AEVUM_TUSD8_FAUCET
    assert faucet_bal.tqday_wei >= 0
    assert faucet_bal.tusd8_units >= 0


def test_http_server_rest_endpoints_and_ui() -> None:
    srv = create_http_server(host="127.0.0.1", port=0, state=AevumAppState())
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=15.0) as client:
            ui = client.get(f"{base}/")
            assert ui.status_code == 200
            assert AEVUM_CHAIN_ID_HEX in ui.text
            assert AEVUM_TUSD8_ADDRESS in ui.text
            assert "function esc(" in ui.text

            health = client.get(f"{base}/api/health").json()
            assert health["status"] == "ok"
            assert health["chain_id"] == 44005
            assert health["tusd8_decimals"] == 6

            net = client.get(f"{base}/api/network").json()
            assert net["reachable"] is True
            assert net["chain_id"] == 44005

            acct = client.get(f"{base}/api/account", params={"address": AEVUM_TUSD8_FAUCET})
            assert acct.status_code == 200
            bad_acct = client.get(f"{base}/api/account", params={"address": "invalid"})
            assert bad_acct.status_code == 400

            guard_resp = client.post(
                f"{base}/api/guard",
                json={
                    "wallet": "0x7099790000000000000000000000000000dc79C8",
                    "handle": "acme.cloud.billing",
                    "legal_name": "Acme Cloud Infrastructure Ltd",
                },
            )
            assert guard_resp.status_code == 200
            assert guard_resp.json()["blocked"] is True

            plan_resp = client.post(
                f"{base}/api/copilot/plan",
                json={
                    "instruction": (
                        "Pay invoice INV-900 for 250.00 tUSD8 to acme.cloud.billing "
                        "(Acme Cloud Infrastructure Ltd) at "
                        "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
                    )
                },
            )
            assert plan_resp.status_code == 200
            assert plan_resp.json()["total_amount_units"] == 250_000_000

            empty_plan = client.post(f"{base}/api/copilot/plan", json={"instruction": ""})
            assert empty_plan.status_code == 400

            oversized = client.post(
                f"{base}/api/copilot/plan",
                content=b'{"instruction":"' + (b"x" * 70_000) + b'"}',
                headers={"Content-Type": "application/json"},
            )
            assert oversized.status_code == 413
            assert oversized.json()["error"] == "payload_too_large"
    finally:
        srv.shutdown()
        srv.server_close()


def test_cli_entrypoints_execute_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    rc_guard = cli_main(
        [
            "guard",
            "--wallet",
            "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "--handle",
            "acme.cloud.billing",
            "--legal-name",
            "Acme Cloud Infrastructure Ltd",
        ]
    )
    assert rc_guard == 0
    guard_out = json.loads(capsys.readouterr().out)
    assert guard_out["risk_level"] == "SAFE"

    rc_net = cli_main(["network"])
    assert rc_net == 0
    net_out = json.loads(capsys.readouterr().out)
    assert net_out["chain_id"] == 44005


def test_copilot_nested_parentheses_and_tranche_comma_parsing() -> None:
    # 1. Nested parentheses in legal names
    plan_apac = build_copilot_payment_plan(
        "Pay invoice INV-2026-901 for 1,250.50 tUSD8 to acme.cloud.billing "
        "(Acme Cloud (APAC) Ltd) at 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 "
        "with 2% early discount, due in 14 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_apac.payee_legal_name == "Acme Cloud (APAC) Ltd"
    assert plan_apac.early_discount_bps == 200
    assert plan_apac.total_amount_units == 1_250_500_000

    plan_sg = build_copilot_payment_plan(
        "Create milestone escrow MSA-2026-902 for 4,000 tUSD8 to qday.logistics.apac "
        "(QDay Logistics (SG) Pte Ltd) at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC "
        "with tranches: 1500, 2500 over 21 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_sg.payee_legal_name == "QDay Logistics (SG) Pte Ltd"

    # 2. (2% early discount) preceding legal name
    plan_disc_pre = build_copilot_payment_plan(
        "Pay invoice INV-2026-903 for 500 tUSD8 to acme.cloud.billing (2% early discount) "
        "(Acme Cloud (APAC) Ltd) at 0x70997970C51812dc3A010C7d01b50e0d17dc79C8, due in 10 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_disc_pre.payee_legal_name == "Acme Cloud (APAC) Ltd"
    assert plan_disc_pre.early_discount_bps == 200

    # 3. (2% early discount) following legal name
    plan_disc_post = build_copilot_payment_plan(
        "Pay invoice INV-2026-904 for 500 tUSD8 to acme.cloud.billing (Acme Cloud (APAC) Ltd) "
        "(2% early discount) at 0x70997970C51812dc3A010C7d01b50e0d17dc79C8, due in 10 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_disc_post.payee_legal_name == "Acme Cloud (APAC) Ltd"
    assert plan_disc_post.early_discount_bps == 200

    # 4. Tranches with commas as thousands separators e.g. (1,250.50, 2,500.00)
    plan_comma_tranches_paren = build_copilot_payment_plan(
        "Create milestone escrow MSA-2026-905 for 3,750.50 tUSD8 to qday.logistics.apac "
        "at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC with "
        "tranches (1,250.50, 2,500.00) over 21 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_comma_tranches_paren.tranche_amounts_units == [1_250_500_000, 2_500_000_000]
    assert plan_comma_tranches_paren.total_amount_units == 3_750_500_000

    # 5. Tranches with brackets e.g. [1,250.50, 2,500.00]
    plan_comma_tranches_bracket = build_copilot_payment_plan(
        "Create milestone escrow MSA-2026-906 for 3,750.50 tUSD8 to qday.logistics.apac "
        "at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC with "
        "tranches: [1,250.50, 2,500.00] over 21 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_comma_tranches_bracket.tranche_amounts_units == [1_250_500_000, 2_500_000_000]
    assert plan_comma_tranches_bracket.total_amount_units == 3_750_500_000

    # 6. Tranches with colon e.g. tranches: 1,250.50, 2,500.00 over 21 days
    plan_comma_tranches_colon = build_copilot_payment_plan(
        "Create milestone escrow MSA-2026-907 for 3,750.50 tUSD8 to qday.logistics.apac "
        "at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC with "
        "tranches: 1,250.50, 2,500.00 over 21 days.",
        now_timestamp=1_765_000_000,
    )
    assert plan_comma_tranches_colon.tranche_amounts_units == [1_250_500_000, 2_500_000_000]
    assert plan_comma_tranches_colon.total_amount_units == 3_750_500_000


def test_server_security_limits_and_concurrency() -> None:
    import concurrent.futures
    import socket

    srv = create_http_server(host="127.0.0.1", port=0, state=AevumAppState())
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def _send_raw_http(raw_bytes: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as s:
            s.sendall(raw_bytes)
            s.shutdown(socket.SHUT_WR)
            res = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                res += chunk
            return res

    try:
        with httpx.Client(timeout=15.0) as client:
            # 1. Content-Length > 65536 returns 413
            oversized_payload = b'{"instruction":"' + (b"A" * 66_000) + b'"}'
            r_413 = client.post(
                f"{base}/api/copilot/plan",
                content=oversized_payload,
                headers={"Content-Type": "application/json"},
            )
            assert r_413.status_code == 413
            assert r_413.json()["error"] == "payload_too_large"

            # 2. Invalid non-integer Content-Length returns 400 (tested via raw socket)
            raw_nan = (
                b"POST /api/copilot/plan HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: not_a_number\r\n"
                b"Content-Type: application/json\r\n"
                b"Connection: close\r\n\r\n"
                b'{"instruction":"test"}'
            )
            res_nan = _send_raw_http(raw_nan)
            assert b"400 Bad Request" in res_nan
            assert b'"bad_request"' in res_nan

            # 3. Negative Content-Length returns 400 (tested via raw socket)
            raw_neg = (
                b"POST /api/copilot/plan HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Length: -10\r\n"
                b"Content-Type: application/json\r\n"
                b"Connection: close\r\n\r\n"
                b'{"instruction":"test"}'
            )
            res_neg = _send_raw_http(raw_neg)
            assert b"400 Bad Request" in res_neg
            assert b'"bad_request"' in res_neg

            # 4. Malformed UTF-8 bytes returns 400
            r_bad_utf8 = client.post(
                f"{base}/api/copilot/plan",
                content=b"\xff\xfe\xfd",
                headers={"Content-Type": "application/json"},
            )
            assert r_bad_utf8.status_code == 400
            assert r_bad_utf8.json()["error"] == "bad_request"

            # 5. Malformed JSON returns 400
            r_bad_json = client.post(
                f"{base}/api/copilot/plan",
                content=b"{invalid json: true}",
                headers={"Content-Type": "application/json"},
            )
            assert r_bad_json.status_code == 400
            assert r_bad_json.json()["error"] == "bad_request"

            # 6. JSON not an object returns 400
            r_not_obj = client.post(
                f"{base}/api/copilot/plan",
                content=b'["array", "not", "object"]',
                headers={"Content-Type": "application/json"},
            )
            assert r_not_obj.status_code == 400
            assert r_not_obj.json()["error"] == "bad_request"

            # 7. Unknown POST route returns 404
            r_not_found = client.post(
                f"{base}/api/non_existent_route",
                json={"key": "val"},
            )
            assert r_not_found.status_code == 404
            assert r_not_found.json()["error"] == "not_found"

            # 8. Concurrent requests execution
            def _worker_req(idx: int) -> int:
                with httpx.Client(timeout=10.0) as sub_client:
                    if idx % 2 == 0:
                        resp = sub_client.get(f"{base}/api/health")
                    else:
                        resp = sub_client.post(
                            f"{base}/api/guard",
                            json={
                                "wallet": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
                                "handle": "acme.cloud.billing",
                                "legal_name": "Acme Cloud Infrastructure Ltd",
                            },
                        )
                    return resp.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(_worker_req, range(16)))
            assert all(code == 200 for code in results)
    finally:
        srv.shutdown()
        srv.server_close()
