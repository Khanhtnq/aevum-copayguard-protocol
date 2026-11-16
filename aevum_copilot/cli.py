"""CLI entrypoint for Aevum CopayGuard & Invoice Escrow Protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aevum_copilot import AEVUM_RPC_URL
from aevum_copilot.copilot import build_copilot_payment_plan
from aevum_copilot.guard import evaluate_payee_guard
from aevum_copilot.rpc import AevumRpcClient
from aevum_copilot.server import AevumAppState, create_http_server

DEMO_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "verified_invoice_early_discount",
        "Pay invoice INV-2026-884 for 1,250.50 tUSD8 to acme.cloud.billing "
        "(Acme Cloud Infrastructure Ltd) at 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 "
        "with 2% early discount, due in 14 days.",
    ),
    (
        "milestone_escrow_contract",
        "Create milestone escrow MSA-2026-310 for 4,000 tUSD8 to qday.logistics.apac "
        "(QDay APAC Logistics Pte Ltd) at 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC "
        "with tranches: 1500, 2500 over 21 days.",
    ),
    (
        "payroll_vesting_stream",
        "Open payroll stream PAY-2026-09 for 6,000.00 tUSD8 to alice.eng.payroll "
        "(Alice Nguyen) at 0x90F79bf6EB2c4f870365E785982E1f101E93b906 over 30 days "
        "with 7-day cliff.",
    ),
    (
        "address_poisoning_attack_blocked",
        "Pay invoice INV-2026-999 for 5,000 tUSD8 to acme.cloud.billing "
        "(Acme Cloud Infrastructure Ltd) at 0x7099790000000000000000000000000000dc79C8.",
    ),
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aevum-copilot")
    sub = parser.add_subparsers(dest="command", required=True)

    net = sub.add_parser("network", help="Probe live QDay Aevum Testnet (Chain ID 44005)")
    net.add_argument("--rpc-url", default=AEVUM_RPC_URL)

    plan = sub.add_parser("plan", help="Parse natural-language tUSD8 payment into ABI calldata")
    plan.add_argument("instruction")
    plan.add_argument("--live-ollama", action="store_true")
    plan.add_argument("--model", default="qwen2.5:7b-instruct")

    guard = sub.add_parser("guard", help="Evaluate Confirmation-of-Payee & poisoning risk")
    guard.add_argument("--wallet", required=True)
    guard.add_argument("--handle", required=True)
    guard.add_argument("--legal-name", default="")

    demo = sub.add_parser("demo", help="Run full 4-scenario Aevum CopayGuard + live RPC demo")
    demo.add_argument("--live-ollama", action="store_true")
    demo.add_argument("--model", default="qwen2.5:7b-instruct")
    demo.add_argument("--output", default="")

    serve = sub.add_parser("serve", help="Start interactive Web UI & JSON API server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8088)
    serve.add_argument("--live-ollama", action="store_true")
    serve.add_argument("--model", default="qwen2.5:7b-instruct")
    return parser


def _run_demo(live_ollama: bool, model: str, output_path: str) -> int:
    rpc = AevumRpcClient()
    network = rpc.fetch_network_snapshot()
    scenarios = [
        {
            "scenario_id": name,
            "instruction": prompt,
            "plan": build_copilot_payment_plan(
                prompt,
                use_ollama=live_ollama,
                ollama_model=model,
                now_timestamp=1_765_000_000,
            ).model_dump(),
        }
        for name, prompt in DEMO_PROMPTS
    ]
    bundle = {
        "network_snapshot": network.model_dump(),
        "scenarios": scenarios,
    }
    rendered = json.dumps(bundle, indent=2)
    if output_path:
        Path(output_path).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "network":
        snap = AevumRpcClient(rpc_url=args.rpc_url).fetch_network_snapshot()
        print(json.dumps(snap.model_dump(), indent=2))
        return 0 if snap.reachable else 1
    if args.command == "plan":
        res = build_copilot_payment_plan(
            args.instruction,
            use_ollama=args.live_ollama,
            ollama_model=args.model,
        )
        print(json.dumps(res.model_dump(), indent=2))
        return 0
    if args.command == "guard":
        rep = evaluate_payee_guard(
            candidate_wallet=args.wallet,
            payee_handle=args.handle,
            legal_name=args.legal_name,
        )
        print(json.dumps(rep.model_dump(), indent=2))
        return 0
    if args.command == "demo":
        return _run_demo(args.live_ollama, args.model, args.output)
    if args.command == "serve":
        state = AevumAppState(use_ollama=args.live_ollama, ollama_model=args.model)
        srv = create_http_server(host=args.host, port=args.port, state=state)
        print(f"Aevum CopayGuard UI listening on http://{args.host}:{args.port}")
        srv.serve_forever()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
