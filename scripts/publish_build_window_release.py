"""Build-window-gated release & optional live Aevum deployment helper (>= 2026-11-16T06:00:00Z)."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

BUILD_WINDOW_OPEN_UTC = datetime(2026, 11, 16, 6, 0, 0, tzinfo=UTC)
BUILD_WINDOW_CLOSE_UTC = datetime(2026, 12, 11, 8, 0, 0, tzinfo=UTC)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Strict OPSEC patterns forbidden in any tracked public file
_HOST_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[a-zA-Z]:\\Users\\", re.IGNORECASE),
    re.compile(r"BOUTY\s*ARGON", re.IGNORECASE),
)

# Canonical known public/test addresses allowed in source/tests
_ALLOWED_PUBLIC_EVM_ADDRESSES: frozenset[str] = frozenset(
    addr.lower()
    for addr in (
        "0x0000000000000000000000000000000000000000",
        "0x456569838f1d43009a91eba4263147afd7233483",  # Official Aevum tUSD8
        "0x33d19bde2e1798c13808f17bfba1780b28e4bacc",  # Official Aevum tQDAY Faucet
        "0x4551b2a3de4f43e6613e23edf798ab4fb0211564",  # Official Aevum tUSD8 Faucet
        "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",  # Anvil demo payee #1
        "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",  # Anvil demo payee #2
        "0x90f79bf6eb2c4f870365e785982e1f101e93b906",  # Anvil demo payee #3
        "0x7099790000000000000000000000000000dc79c8",  # Synthetic poisoning test vector
        "0x1111111111111111111111111111111111114405",  # Default vault placeholder
        "0x2222222222222222222222222222222222224405",  # Default arbiter placeholder
        "0x1234567890123456789012345678901234567890",  # Synthetic test vector
        "0x1234567890abcdef1234567890abcdef12345678",  # Synthetic checksum test vector
    )
)

_EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_EXCLUDED_DIRS = {
    ".git",
    "lib",
    "out",
    "cache",
    "broadcast",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
_EXCLUDED_FILES = {"SUBMISSION_PACKAGE.md", ".env"}


def check_build_window(now_utc: datetime | None = None) -> None:
    current = now_utc or datetime.now(UTC)
    if current < BUILD_WINDOW_OPEN_UTC:
        raise RuntimeError(
            f"Refusing to publish before official Aevum Builder Challenge #1 window opens! "
            f"Current UTC: {current.isoformat()} < Gate UTC: {BUILD_WINDOW_OPEN_UTC.isoformat()}"
        )
    if current > BUILD_WINDOW_CLOSE_UTC:
        raise RuntimeError(
            f"Aevum Builder Challenge #1 submission window closed at "
            f"{BUILD_WINDOW_CLOSE_UTC.isoformat()} (current: {current.isoformat()})"
        )


def verify_opsec_and_gitignore(root: Path = REPO_ROOT) -> list[Path]:
    gitignore_text = (root / ".gitignore").read_text(encoding="utf-8")
    if "SUBMISSION_PACKAGE.md" not in gitignore_text or ".env" not in gitignore_text:
        raise RuntimeError(".gitignore must exclude both SUBMISSION_PACKAGE.md and .env")

    scanned: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.name in _EXCLUDED_FILES:
            continue
        scanned.append(rel)
        content = path.read_text(encoding="utf-8", errors="ignore")
        for pat in _HOST_PATH_PATTERNS:
            if pat.search(content):
                raise RuntimeError(f"Forbidden host path pattern found in {rel}")
        for match in _EVM_ADDRESS_RE.findall(content):
            if match.lower() not in _ALLOWED_PUBLIC_EVM_ADDRESSES:
                raise RuntimeError(
                    f"Unallowlisted EVM wallet address {match} found in public file {rel}"
                )
    return scanned


def _init_and_push_git(remote_url: str, commit_message: str) -> None:
    if not (REPO_ROOT / ".git").exists():
        subprocess.run(["git", "-C", str(REPO_ROOT), "init", "-b", "main"], check=True)
    else:
        has_head = (
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
            ).returncode
            == 0
        )
        if has_head:
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "checkout", "--orphan", "_aevum_release_main"],
                check=True,
            )
    subprocess.run(["git", "-C", str(REPO_ROOT), "add", "-A"], check=True)
    status = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "SUBMISSION_PACKAGE.md" in status or ".env\n" in status:
        raise RuntimeError("ABORT: Private file appeared in git status!")
    subprocess.run(["git", "-C", str(REPO_ROOT), "commit", "-m", commit_message], check=True)
    subprocess.run(["git", "-C", str(REPO_ROOT), "branch", "-M", "main"], check=True)
    if remote_url:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "push", "-u", remote_url, "main"],
            check=True,
        )


def _find_forge_binary() -> str:
    found = shutil.which("forge")
    if found:
        return found
    home_forge = Path.home() / ".foundry" / "bin" / "forge.exe"
    if home_forge.exists():
        return str(home_forge)
    posix_forge = Path.home() / ".foundry" / "bin" / "forge"
    if posix_forge.exists():
        return str(posix_forge)
    return "forge"


def _broadcast_contracts() -> None:
    forge_bin = _find_forge_binary()
    subprocess.run(
        [
            forge_bin,
            "script",
            "script/DeployAndProveAevum.s.sol:DeployAndProveAevum",
            "--rpc-url",
            "https://rpc-test.qday.info",
            "--broadcast",
            "-vvv",
        ],
        cwd=str(REPO_ROOT),
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-only", action="store_true", help="Run OPSEC & gitignore checks only"
    )
    parser.add_argument("--remote-url", default="", help="GitHub HTTPS remote URL to push main")
    parser.add_argument(
        "--broadcast-deploy", action="store_true", help="Broadcast Foundry deployment"
    )
    parser.add_argument(
        "--commit-message",
        default="feat: Aevum CopayGuard & 6-decimal tUSD8 Invoice Escrow Protocol",
    )
    args = parser.parse_args(argv)

    scanned = verify_opsec_and_gitignore(REPO_ROOT)
    print(
        f"OPSEC audit clean across {len(scanned)} public files (SUBMISSION_PACKAGE.md gitignored)."
    )

    if args.audit_only:
        return 0

    check_build_window()
    if args.broadcast_deploy:
        _broadcast_contracts()
    _init_and_push_git(args.remote_url, args.commit_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
