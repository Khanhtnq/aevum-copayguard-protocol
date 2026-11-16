"""Unit tests for the Aevum Builder Challenge #1 anti-disqualification launch gate."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts.publish_build_window_release import (
    BUILD_WINDOW_CLOSE_UTC,
    BUILD_WINDOW_OPEN_UTC,
    check_build_window,
)
from scripts.publish_build_window_release import (
    main as release_main,
)


def test_build_window_constants_match_challenge_specs() -> None:
    assert BUILD_WINDOW_OPEN_UTC == datetime(2026, 11, 16, 6, 0, 0, tzinfo=UTC)
    assert BUILD_WINDOW_CLOSE_UTC == datetime(2026, 12, 11, 8, 0, 0, tzinfo=UTC)


def test_check_build_window_blocks_pre_window_timestamps() -> None:
    # 1. Blocked during initial preparation window
    wave_ts = datetime(2026, 11, 13, 7, 0, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="Refusing to publish before official"):
        check_build_window(wave_ts)

    # 2. Blocked 1 second before official opening gate
    one_sec_before = datetime(2026, 11, 16, 5, 59, 59, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="Refusing to publish before official"):
        check_build_window(one_sec_before)


def test_check_build_window_allows_valid_window() -> None:
    # 1. Exactly at opening gate
    open_gate = datetime(2026, 11, 16, 6, 0, 0, tzinfo=UTC)
    check_build_window(open_gate)

    # 2. During active competition
    mid_window = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
    check_build_window(mid_window)

    # 3. Exactly at closing gate
    close_gate = datetime(2026, 12, 11, 8, 0, 0, tzinfo=UTC)
    check_build_window(close_gate)


def test_check_build_window_blocks_post_window_timestamps() -> None:
    one_sec_after = datetime(2026, 12, 11, 8, 0, 1, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="submission window closed"):
        check_build_window(one_sec_after)


def test_release_main_audit_only_succeeds_pre_window(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = release_main(["--audit-only"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert "OPSEC audit clean" in captured
