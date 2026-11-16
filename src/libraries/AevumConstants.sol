// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

library AevumConstants {
    uint256 internal constant AEVUM_CHAIN_ID = 44005;
    address internal constant AEVUM_TUSD8 = 0x456569838f1d43009A91ebA4263147Afd7233483;
    uint8 internal constant TUSD8_DECIMALS = 6;
    uint256 internal constant TUSD8_UNIT = 1_000_000;
    uint16 internal constant BPS_DENOMINATOR = 10_000;
    uint16 internal constant MAX_DISCOUNT_BPS = 5_000;
    uint256 internal constant MAX_MILESTONES = 10;

    address internal constant AEVUM_TQDAY_FAUCET = 0x33d19Bde2e1798c13808f17BfBa1780b28e4bACC;
    address internal constant AEVUM_TUSD8_FAUCET = 0x4551B2a3dE4F43e6613E23edf798Ab4FB0211564;
}
