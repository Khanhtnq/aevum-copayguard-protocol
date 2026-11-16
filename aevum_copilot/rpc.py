"""Live JSON-RPC inspector for QDay Aevum Testnet (Chain ID 44005) and 6-decimal tUSD8."""

from __future__ import annotations

from decimal import Decimal

import httpx

from aevum_copilot import (
    AEVUM_CHAIN_ID,
    AEVUM_RPC_URL,
    AEVUM_TUSD8_ADDRESS,
    TUSD8_DECIMALS,
)
from aevum_copilot.abi import encode_verify_payee_match
from aevum_copilot.keccak import function_selector, to_checksum_address
from aevum_copilot.models import (
    AccountBalanceSnapshot,
    AevumNetworkSnapshot,
    format_tusd8_units,
)


def _decode_abi_string(hex_data: str) -> str:
    raw = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 <= len(raw):
            length = int.from_bytes(raw[offset : offset + 32], "big")
            start = offset + 32
            return raw[start : start + length].decode("utf-8", errors="replace")
    return raw.rstrip(b"\x00").decode("utf-8", errors="replace")


def _decode_uint(hex_data: str) -> int:
    cleaned = hex_data.strip().removeprefix("0x")
    return int(cleaned, 16) if cleaned else 0


class AevumRpcClient:
    def __init__(
        self,
        rpc_url: str = AEVUM_RPC_URL,
        tusd8_address: str = AEVUM_TUSD8_ADDRESS,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.tusd8_address = to_checksum_address(tusd8_address)
        self.timeout_seconds = timeout_seconds

    def _rpc(self, method: str, params: list[object]) -> str:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.post(self.rpc_url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        if "error" in body and body["error"]:
            raise RuntimeError(f"RPC {method} error: {body['error']}")
        return str(body.get("result") or "0x0")

    def eth_call(self, to: str, data: str) -> str:
        return self._rpc("eth_call", [{"to": to_checksum_address(to), "data": data}, "latest"])

    def fetch_network_snapshot(self) -> AevumNetworkSnapshot:
        try:
            chain_id = _decode_uint(self._rpc("eth_chainId", []))
            block_num = _decode_uint(self._rpc("eth_blockNumber", []))
            gas_price = _decode_uint(self._rpc("eth_gasPrice", []))
            sym_hex = self.eth_call(self.tusd8_address, function_selector("symbol()"))
            dec_hex = self.eth_call(self.tusd8_address, function_selector("decimals()"))
            sup_hex = self.eth_call(self.tusd8_address, function_selector("totalSupply()"))
            symbol = _decode_abi_string(sym_hex) or "tUSD8"
            decimals = _decode_uint(dec_hex)
            supply = _decode_uint(sup_hex)
            return AevumNetworkSnapshot(
                rpc_url=self.rpc_url,
                reachable=True,
                chain_id=chain_id,
                expected_chain_id=AEVUM_CHAIN_ID,
                latest_block=block_num,
                gas_price_wei=gas_price,
                tusd8_address=self.tusd8_address,
                tusd8_symbol=symbol,
                tusd8_decimals=decimals,
                tusd8_total_supply_units=supply,
                tusd8_total_supply_formatted=format_tusd8_units(supply),
            )
        except Exception as exc:
            return AevumNetworkSnapshot(
                rpc_url=self.rpc_url,
                reachable=False,
                chain_id=0,
                expected_chain_id=AEVUM_CHAIN_ID,
                latest_block=0,
                gas_price_wei=0,
                tusd8_address=self.tusd8_address,
                tusd8_symbol="tUSD8",
                tusd8_decimals=TUSD8_DECIMALS,
                tusd8_total_supply_units=0,
                tusd8_total_supply_formatted="0.000000",
                error=str(exc),
            )

    def fetch_account_balances(self, address: str) -> AccountBalanceSnapshot:
        checksummed = to_checksum_address(address)
        tqday_wei = _decode_uint(self._rpc("eth_getBalance", [checksummed, "latest"]))
        bal_selector = function_selector("balanceOf(address)")
        padded_addr = checksummed[2:].lower().rjust(64, "0")
        tusd8_units = _decode_uint(self.eth_call(self.tusd8_address, bal_selector + padded_addr))
        tqday_dec = (Decimal(tqday_wei) / Decimal(10**18)).quantize(Decimal("0.000001"))
        return AccountBalanceSnapshot(
            address=checksummed,
            tqday_wei=tqday_wei,
            tqday_formatted=f"{tqday_dec:.6f}",
            tusd8_units=tusd8_units,
            tusd8_formatted=format_tusd8_units(tusd8_units),
        )

    def check_onchain_payee(
        self,
        registry_address: str,
        wallet: str,
        handle_hash: str,
        legal_name_hash: str,
    ) -> bool:
        calldata = encode_verify_payee_match(wallet, handle_hash, legal_name_hash)
        res = self.eth_call(registry_address, calldata)
        return _decode_uint(res) == 1
