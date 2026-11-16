// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20Metadata} from "../src/interfaces/IERC20Metadata.sol";
import {AevumConstants} from "../src/libraries/AevumConstants.sol";
import {AevumPayeeRegistry} from "../src/AevumPayeeRegistry.sol";
import {AevumInvoiceVault} from "../src/AevumInvoiceVault.sol";

/// @notice End-to-end fork verification against the live QDay Aevum Testnet (Chain ID 44005)
///         and the real deployed 6-decimal tUSD8 token at 0x456569838f1d43009A91ebA4263147Afd7233483.
contract AevumLiveForkTest is Test {
    IERC20Metadata internal tusd8 = IERC20Metadata(AevumConstants.AEVUM_TUSD8);
    AevumPayeeRegistry internal registry;
    AevumInvoiceVault internal vault;

    address internal payer = makeAddr("aevumLivePayer");
    address internal supplier = makeAddr("aevumLiveSupplier");
    address internal contractor = makeAddr("aevumLiveContractor");
    address internal arbiter = makeAddr("aevumLiveArbiter");

    bytes32 internal constant SUPPLIER_HANDLE = keccak256("qday.logistics.apac");
    bytes32 internal constant SUPPLIER_LEGAL = keccak256("QDay APAC Logistics Pte Ltd");
    bytes32 internal constant CONTRACTOR_HANDLE = keccak256("dev.audit.partner");
    bytes32 internal constant CONTRACTOR_LEGAL = keccak256("Sentinel Security Partners");

    function setUp() public {
        string memory rpcUrl = vm.envOr("AEVUM_RPC_URL", string("https://rpc-test.qday.info"));
        vm.createSelectFork(rpcUrl);

        assertEq(block.chainid, AevumConstants.AEVUM_CHAIN_ID);
        assertEq(tusd8.decimals(), AevumConstants.TUSD8_DECIMALS);
        assertEq(keccak256(bytes(tusd8.symbol())), keccak256(bytes("tUSD8")));

        registry = new AevumPayeeRegistry();
        vault = new AevumInvoiceVault(AevumConstants.AEVUM_TUSD8, address(registry));

        vm.prank(supplier);
        registry.registerPayee(SUPPLIER_HANDLE, SUPPLIER_LEGAL, keccak256("ipfs://qday-supplier"));

        vm.prank(contractor);
        registry.registerPayee(
            CONTRACTOR_HANDLE,
            CONTRACTOR_LEGAL,
            keccak256("ipfs://qday-contractor")
        );

        deal(AevumConstants.AEVUM_TUSD8, payer, 50_000 * AevumConstants.TUSD8_UNIT);
        vm.prank(payer);
        tusd8.approve(address(vault), type(uint256).max);
    }

    function test_LiveAevumFork_EndToEndInvoiceEscrowAndPayrollFlow() public {
        // 1. Live tUSD8 Confirmation-of-Payee Invoice with 1.50% early-payment discount
        uint256 invoicePrincipal = 2_000 * AevumConstants.TUSD8_UNIT; // 2,000.000000 tUSD8
        vm.prank(supplier);
        uint256 invoiceId = vault.createInvoice(
            payer,
            SUPPLIER_HANDLE,
            invoicePrincipal,
            150, // 1.50% -> 30.000000 tUSD8 discount -> 1,970.000000 tUSD8
            uint64(block.timestamp + 3 days),
            uint64(block.timestamp + 30 days),
            keccak256("AEVUM-LIVE-INV-001")
        );

        vm.prank(payer);
        (uint256 settled, bytes32 receipt) = vault.payVerifiedInvoice(
            invoiceId,
            supplier,
            SUPPLIER_HANDLE,
            SUPPLIER_LEGAL,
            invoicePrincipal
        );
        assertEq(settled, 1_970 * AevumConstants.TUSD8_UNIT);
        assertTrue(receipt != bytes32(0));
        assertEq(tusd8.balanceOf(supplier), 1_970 * AevumConstants.TUSD8_UNIT);

        // 2. Live tUSD8 Milestone Escrow on Chain 44005
        uint256[] memory tranches = new uint256[](2);
        tranches[0] = 1_500 * AevumConstants.TUSD8_UNIT;
        tranches[1] = 2_500 * AevumConstants.TUSD8_UNIT;

        vm.prank(payer);
        uint256 escrowId = vault.createMilestoneEscrow(
            contractor,
            CONTRACTOR_HANDLE,
            CONTRACTOR_LEGAL,
            arbiter,
            tranches,
            uint64(block.timestamp + 14 days),
            keccak256("AEVUM-LIVE-ESCROW-001")
        );

        vm.prank(payer);
        vault.releaseMilestone(escrowId, 0);
        vm.prank(payer);
        vault.releaseMilestone(escrowId, 1);
        assertEq(tusd8.balanceOf(contractor), 4_000 * AevumConstants.TUSD8_UNIT);

        // 3. Live tUSD8 Linear Payroll Stream on Chain 44005
        uint64 startAt = uint64(block.timestamp);
        uint64 endAt = startAt + 30 days;
        vm.prank(payer);
        uint256 streamId = vault.createPayrollStream(
            contractor,
            CONTRACTOR_HANDLE,
            CONTRACTOR_LEGAL,
            3_000 * AevumConstants.TUSD8_UNIT,
            startAt,
            startAt,
            endAt,
            true
        );

        vm.warp(endAt);
        vm.prank(contractor);
        uint256 claimed = vault.claimStream(streamId);
        assertEq(claimed, 3_000 * AevumConstants.TUSD8_UNIT);
        assertEq(tusd8.balanceOf(contractor), 7_000 * AevumConstants.TUSD8_UNIT);
        assertEq(tusd8.balanceOf(address(vault)), 0);
    }
}
