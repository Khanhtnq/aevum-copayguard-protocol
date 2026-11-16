// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {IERC20Metadata} from "../src/interfaces/IERC20Metadata.sol";
import {AevumConstants} from "../src/libraries/AevumConstants.sol";
import {AevumPayeeRegistry} from "../src/AevumPayeeRegistry.sol";
import {AevumInvoiceVault} from "../src/AevumInvoiceVault.sol";

/// @notice Deploys AevumPayeeRegistry + AevumInvoiceVault onto QDay Aevum Testnet (Chain ID 44005)
///         and executes a live Confirmation-of-Payee tUSD8 micro-settlement when deployer holds >= 0.1 tUSD8.
contract DeployAndProveAevum is Script {
    function run()
        external
        returns (AevumPayeeRegistry registry, AevumInvoiceVault vault)
    {
        uint256 deployerKey = vm.envUint("AEVUM_DEPLOYER_PRIVATE_KEY");
        address deployer = vm.addr(deployerKey);
        address tokenAddr = vm.envOr("AEVUM_TUSD8_ADDRESS", AevumConstants.AEVUM_TUSD8);

        vm.startBroadcast(deployerKey);

        registry = new AevumPayeeRegistry();
        vault = new AevumInvoiceVault(tokenAddr, address(registry));

        bytes32 handleHash = keccak256("aevum.treasury.ops");
        bytes32 legalNameHash = keccak256("Aevum CopayGuard Demo Treasury");
        bytes32 metadataHash = keccak256("ipfs://aevum-copayguard-v1");
        registry.registerPayee(handleHash, legalNameHash, metadataHash);

        IERC20Metadata token = IERC20Metadata(tokenAddr);
        uint256 tusd8Bal = token.balanceOf(deployer);
        uint256 sampleAmount = 100_000; // 0.100000 tUSD8 (6 decimals)
        if (tusd8Bal >= sampleAmount) {
            token.approve(address(vault), sampleAmount);
            uint256 invoiceId = vault.createInvoice(
                deployer,
                handleHash,
                sampleAmount,
                0,
                0,
                uint64(block.timestamp + 7 days),
                keccak256("INV-AEVUM-BOOT-001")
            );
            vault.payVerifiedInvoice(
                invoiceId,
                deployer,
                handleHash,
                legalNameHash,
                sampleAmount
            );
        }

        vm.stopBroadcast();

        console2.log("Chain ID:", block.chainid);
        console2.log("tUSD8 Token:", tokenAddr);
        console2.log("AevumPayeeRegistry:", address(registry));
        console2.log("AevumInvoiceVault:", address(vault));
    }
}
