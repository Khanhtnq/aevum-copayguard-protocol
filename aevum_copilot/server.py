"""HTTP REST API & interactive dashboard server for Aevum CopayGuard & Invoice Escrow."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aevum_copilot import (
    AEVUM_CHAIN_ID,
    AEVUM_CHAIN_ID_HEX,
    AEVUM_EXPLORER_URL,
    AEVUM_FAUCET_URL,
    AEVUM_NETWORK_NAME,
    AEVUM_RPC_URL,
    AEVUM_TUSD8_ADDRESS,
    TUSD8_DECIMALS,
    TUSD8_SYMBOL,
)
from aevum_copilot.copilot import (
    DEFAULT_VAULT_PLACEHOLDER,
    build_copilot_payment_plan,
)
from aevum_copilot.guard import DEFAULT_DEMO_DIRECTORY, evaluate_payee_guard
from aevum_copilot.rpc import AevumRpcClient

_STATIC_INDEX = Path(__file__).with_name("static") / "index.html"


class AevumAppState:
    def __init__(
        self,
        *,
        rpc_url: str = AEVUM_RPC_URL,
        tusd8_address: str = AEVUM_TUSD8_ADDRESS,
        vault_address: str = DEFAULT_VAULT_PLACEHOLDER,
        use_ollama: bool = False,
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_model: str = "qwen2.5:7b-instruct",
    ) -> None:
        self.rpc_client = AevumRpcClient(rpc_url=rpc_url, tusd8_address=tusd8_address)
        self.vault_address = vault_address
        self.use_ollama = use_ollama
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model


def _build_health_payload(state: AevumAppState) -> dict[str, object]:
    return {
        "status": "ok",
        "service": "aevum-copayguard",
        "network": AEVUM_NETWORK_NAME,
        "chain_id": AEVUM_CHAIN_ID,
        "chain_id_hex": AEVUM_CHAIN_ID_HEX,
        "rpc_url": state.rpc_client.rpc_url,
        "explorer_url": AEVUM_EXPLORER_URL,
        "faucet_url": AEVUM_FAUCET_URL,
        "tusd8_address": state.rpc_client.tusd8_address,
        "tusd8_symbol": TUSD8_SYMBOL,
        "tusd8_decimals": TUSD8_DECIMALS,
        "trusted_directory": [c.model_dump() for c in DEFAULT_DEMO_DIRECTORY],
    }


def _dispatch_post_route(
    route_path: str,
    body: dict[str, object],
    state: AevumAppState,
) -> tuple[int, dict[str, object]]:
    if route_path == "/api/guard":
        report = evaluate_payee_guard(
            candidate_wallet=str(body.get("wallet") or ""),
            payee_handle=str(body.get("handle") or ""),
            legal_name=str(body.get("legal_name") or ""),
        )
        return HTTPStatus.OK, report.model_dump()

    if route_path == "/api/copilot/plan":
        instruction = str(body.get("instruction") or "").strip()
        if not instruction:
            raise ValueError("Missing 'instruction' text")
        use_llm = bool(body.get("use_ollama", state.use_ollama))
        vault = str(body.get("vault_address") or state.vault_address)
        plan = build_copilot_payment_plan(
            instruction,
            vault_address=vault,
            token_address=state.rpc_client.tusd8_address,
            use_ollama=use_llm,
            ollama_url=state.ollama_url,
            ollama_model=state.ollama_model,
        )
        return HTTPStatus.OK, plan.model_dump()

    return HTTPStatus.NOT_FOUND, {"error": "not_found"}


class PayloadTooLargeError(Exception):
    """Raised when Content-Length exceeds allowable limit."""


class BadRequestError(Exception):
    """Raised when request payload or headers are malformed."""


class AevumRequestHandler(BaseHTTPRequestHandler):
    app_state: AevumAppState = AevumAppState()

    def log_message(self, fmt: str, *args: object) -> None:
        del fmt, args

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_bytes: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def _read_json_body(self) -> dict[str, object]:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            length = 0
        else:
            try:
                length = int(raw_len.strip())
            except ValueError as exc:
                raise BadRequestError("Invalid non-integer Content-Length header") from exc

        if length < 0:
            raise BadRequestError("Negative Content-Length header is invalid")
        if length > 65_536:
            try:
                self.rfile.read(min(length, 131_072))
            except Exception:
                pass
            self.close_connection = True
            raise PayloadTooLargeError("Request payload size exceeds limit (max 65536 bytes)")

        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadRequestError("Request body must be valid UTF-8") from exc

        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise BadRequestError(f"Malformed JSON request body: {exc.msg}") from exc

        if not isinstance(parsed, dict):
            raise BadRequestError("JSON request body must be an object")
        return parsed

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(_STATIC_INDEX.read_bytes())
            return
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, _build_health_payload(self.app_state))
            return
        if parsed.path == "/api/network":
            snap = self.app_state.rpc_client.fetch_network_snapshot()
            self._send_json(HTTPStatus.OK, snap.model_dump())
            return
        if parsed.path == "/api/account":
            self._handle_account_query(parsed.query)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def _handle_account_query(self, query: str) -> None:
        params = parse_qs(query)
        addr = (params.get("address") or [""])[0]
        try:
            bal = self.app_state.rpc_client.fetch_account_balances(addr)
            self._send_json(HTTPStatus.OK, bal.model_dump())
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
            status, payload = _dispatch_post_route(parsed.path, body, self.app_state)
            self._send_json(status, payload)
        except PayloadTooLargeError as exc:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "payload_too_large", "message": str(exc)},
            )
        except BadRequestError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "bad_request", "message": str(exc)},
            )
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "bad_request", "message": str(exc)},
            )
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_server_error"},
            )


def make_handler(state: AevumAppState) -> type[BaseHTTPRequestHandler]:
    class _BoundHandler(AevumRequestHandler):
        app_state = state

    return _BoundHandler


def create_http_server(
    host: str = "127.0.0.1",
    port: int = 8088,
    state: AevumAppState | None = None,
) -> ThreadingHTTPServer:
    effective_state = state or AevumAppState()
    return ThreadingHTTPServer((host, port), make_handler(effective_state))
