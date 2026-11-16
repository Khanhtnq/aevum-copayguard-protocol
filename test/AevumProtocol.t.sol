// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {IERC20Metadata} from "../src/interfaces/IERC20Metadata.sol";
import {AevumConstants} from "../src/libraries/AevumConstants.sol";
import {SafeERC20Transfer} from "../src/libraries/SafeERC20Transfer.sol";
import {AevumPayeeRegistry} from "../src/AevumPayeeRegistry.sol";
import {AevumInvoiceVault} from "../src/AevumInvoiceVault.sol";

contract MockTUSD8 is IERC20Metadata {
    string public constant name = "Test USD8";
    string public constant symbol = "tUSD8";
    uint8 public immutable override decimals;
    uint256 public override totalSupply;

    mapping(address => uint256) public override balanceOf;
    mapping(address => mapping(address => uint256)) public override allowance;

    constructor(uint8 dec) {
        decimals = dec;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 value) external override returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) public virtual override returns (bool) {
        require(balanceOf[msg.sender] >= value, "insufficient");
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        emit Transfer(msg.sender, to, value);
        return true;
    }

    function transferFrom(
        address from,
        address to,
        uint256 value
    ) public virtual override returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= value && balanceOf[from] >= value, "insufficient");
        allowance[from][msg.sender] = allowed - value;
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
        return true;
    }
}

contract FeeOnTransferMockToken is MockTUSD8 {
    constructor() MockTUSD8(6) {}

    function transferFrom(
        address from,
        address to,
        uint256 value
    ) public override returns (bool) {
        uint256 fee = value / 100;
        uint256 net = value - fee;
        allowance[from][msg.sender] -= value;
        balanceOf[from] -= value;
        balanceOf[to] += net;
        emit Transfer(from, to, net);
        return true;
    }
}

contract ReentrantPayeeToken is MockTUSD8 {
    AevumInvoiceVault public targetVault;
    uint256 public reenterStreamId;
    bool public attackArmed;

    constructor() MockTUSD8(6) {}

    function arm(AevumInvoiceVault vault_, uint256 streamId_) external {
        targetVault = vault_;
        reenterStreamId = streamId_;
        attackArmed = true;
    }

    function transfer(address to, uint256 value) public override returns (bool) {
        bool ok = super.transfer(to, value);
        if (attackArmed) {
            attackArmed = false;
            targetVault.claimStream(reenterStreamId);
        }
        return ok;
    }
}

contract AevumProtocolTest is Test {
    MockTUSD8 internal token;
    AevumPayeeRegistry internal registry;
    AevumInvoiceVault internal vault;

    address internal vendor = makeAddr("vendor");
    address internal vendorRotated = makeAddr("vendorRotated");
    address internal poisonAttacker = makeAddr("poisonAttacker");
    address internal payer = makeAddr("payer");
    address internal arbiter = makeAddr("arbiter");
    address internal employee = makeAddr("employee");

    bytes32 internal constant VENDOR_HANDLE = keccak256("acme.cloud.billing");
    bytes32 internal constant VENDOR_LEGAL = keccak256("Acme Cloud Infrastructure Ltd");
    bytes32 internal constant EMPLOYEE_HANDLE = keccak256("alice.eng.payroll");
    bytes32 internal constant EMPLOYEE_LEGAL = keccak256("Alice Nguyen");

    function setUp() public {
        vm.warp(1_765_000_000);
        token = new MockTUSD8(6);
        registry = new AevumPayeeRegistry();
        vault = new AevumInvoiceVault(address(token), address(registry));

        vm.prank(vendor);
        registry.registerPayee(VENDOR_HANDLE, VENDOR_LEGAL, keccak256("ipfs://acme"));

        vm.prank(employee);
        registry.registerPayee(EMPLOYEE_HANDLE, EMPLOYEE_LEGAL, keccak256("ipfs://alice"));

        token.mint(payer, 500_000 * AevumConstants.TUSD8_UNIT); // 500,000.000000 tUSD8
        vm.prank(payer);
        token.approve(address(vault), type(uint256).max);
    }

    function test_VaultRejectsNonSixDecimalToken() public {
        MockTUSD8 wrongDecToken = new MockTUSD8(18);
        vm.expectRevert(
            abi.encodeWithSelector(AevumInvoiceVault.UnsupportedTokenDecimals.selector, 18, 6)
        );
        new AevumInvoiceVault(address(wrongDecToken), address(registry));
    }

    function test_VerifiedInvoiceEarlyDiscountAndPostDeadlineSettlement() public {
        uint256 principal = 1_250_500_000; // 1,250.500000 tUSD8
        uint64 discountDeadline = uint64(block.timestamp + 2 days);
        uint64 dueAt = uint64(block.timestamp + 14 days);

        vm.prank(vendor);
        uint256 inv1 = vault.createInvoice(
            payer,
            VENDOR_HANDLE,
            principal,
            200, // 2.00% early discount
            discountDeadline,
            dueAt,
            keccak256("INV-2026-1001")
        );

        uint256 vendorBefore = token.balanceOf(vendor);
        vm.prank(payer);
        (uint256 settled1, bytes32 receipt1) = vault.payVerifiedInvoice(
            inv1,
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            principal
        );

        // 2% of 1,250.500000 = 25.010000 -> settled = 1,225.490000 tUSD8
        assertEq(settled1, 1_225_490_000);
        assertTrue(receipt1 != bytes32(0));
        assertEq(token.balanceOf(vendor) - vendorBefore, 1_225_490_000);

        // Second invoice paid after discountDeadline pays full principal
        vm.prank(vendor);
        uint256 inv2 = vault.createInvoice(
            payer,
            VENDOR_HANDLE,
            principal,
            200,
            discountDeadline,
            dueAt,
            keccak256("INV-2026-1002")
        );

        vm.warp(discountDeadline + 1);
        vm.prank(payer);
        (uint256 settled2,) = vault.payVerifiedInvoice(
            inv2,
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            principal
        );
        assertEq(settled2, principal);
    }

    function test_AddressPoisoningAndWalletRotationMismatchBlocked() public {
        vm.prank(vendor);
        uint256 invId = vault.createInvoice(
            payer,
            VENDOR_HANDLE,
            500 * AevumConstants.TUSD8_UNIT,
            0,
            0,
            uint64(block.timestamp + 7 days),
            keccak256("INV-POISON-TEST")
        );

        // 1. Payer tries to pay a poisoned lookalike wallet for VENDOR_HANDLE -> reverts
        vm.prank(payer);
        vm.expectRevert(
            abi.encodeWithSelector(
                AevumPayeeRegistry.PayeeWalletMismatch.selector,
                VENDOR_HANDLE,
                poisonAttacker,
                vendor
            )
        );
        vault.payVerifiedInvoice(
            invId,
            poisonAttacker,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            500 * AevumConstants.TUSD8_UNIT
        );

        // 2. Vendor rotates wallet via 2-step handshake; stale invoice tied to old wallet is blocked
        vm.prank(vendor);
        registry.proposeWalletRotation(VENDOR_HANDLE, vendorRotated);
        vm.prank(vendorRotated);
        registry.acceptWalletRotation(VENDOR_HANDLE);

        vm.prank(payer);
        vm.expectRevert(
            abi.encodeWithSelector(
                AevumPayeeRegistry.PayeeWalletMismatch.selector,
                VENDOR_HANDLE,
                vendor,
                vendorRotated
            )
        );
        vault.payVerifiedInvoice(
            invId,
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            500 * AevumConstants.TUSD8_UNIT
        );
    }

    function test_MilestoneEscrowReleaseAndDisputeResolution() public {
        uint256[] memory tranches = new uint256[](3);
        tranches[0] = 300 * AevumConstants.TUSD8_UNIT;
        tranches[1] = 400 * AevumConstants.TUSD8_UNIT;
        tranches[2] = 300 * AevumConstants.TUSD8_UNIT;

        vm.prank(payer);
        uint256 escId = vault.createMilestoneEscrow(
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            arbiter,
            tranches,
            uint64(block.timestamp + 30 days),
            keccak256("MSA-AEVUM-01")
        );

        vm.prank(payer);
        vault.releaseMilestone(escId, 0);
        assertEq(token.balanceOf(vendor), 300 * AevumConstants.TUSD8_UNIT);

        // Dispute remaining 700 tUSD8 -> arbiter splits 25% payer refund (175 tUSD8), 75% vendor (525 tUSD8)
        vm.prank(vendor);
        vault.raiseDispute(escId, keccak256("partial-delivery-evidence"));

        uint256 payerBefore = token.balanceOf(payer);
        vm.prank(arbiter);
        vault.resolveDispute(escId, 2_500);

        assertEq(token.balanceOf(payer) - payerBefore, 175 * AevumConstants.TUSD8_UNIT);
        assertEq(token.balanceOf(vendor), 825 * AevumConstants.TUSD8_UNIT);
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function test_MilestoneEscrowExpiryReclaim() public {
        uint256[] memory tranches = new uint256[](2);
        tranches[0] = 200 * AevumConstants.TUSD8_UNIT;
        tranches[1] = 300 * AevumConstants.TUSD8_UNIT;
        uint64 expiresAt = uint64(block.timestamp + 5 days);

        vm.prank(payer);
        uint256 escId = vault.createMilestoneEscrow(
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            arbiter,
            tranches,
            expiresAt,
            keccak256("MSA-EXPIRY")
        );

        vm.warp(expiresAt + 1);
        uint256 payerBefore = token.balanceOf(payer);
        vm.prank(payer);
        vault.reclaimExpiredEscrow(escId);
        assertEq(token.balanceOf(payer) - payerBefore, 500 * AevumConstants.TUSD8_UNIT);
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function test_PayrollStreamCliffLinearVestingAndCancel() public {
        uint64 startAt = uint64(block.timestamp);
        uint64 cliffAt = startAt + 10 days;
        uint64 endAt = startAt + 100 days;
        uint256 deposit = 10_000 * AevumConstants.TUSD8_UNIT; // 10,000.000000 tUSD8

        vm.prank(payer);
        uint256 streamId = vault.createPayrollStream(
            employee,
            EMPLOYEE_HANDLE,
            EMPLOYEE_LEGAL,
            deposit,
            startAt,
            cliffAt,
            endAt,
            true
        );

        // Before cliff: 0 claimable
        vm.warp(startAt + 5 days);
        assertEq(vault.claimableStream(streamId), 0);
        vm.prank(employee);
        vm.expectRevert(
            abi.encodeWithSelector(AevumInvoiceVault.NothingClaimable.selector, streamId)
        );
        vault.claimStream(streamId);

        // At 25 days: 2,500 tUSD8 vested
        vm.warp(startAt + 25 days);
        vm.prank(employee);
        uint256 claimed = vault.claimStream(streamId);
        assertEq(claimed, 2_500 * AevumConstants.TUSD8_UNIT);

        // At 60 days: employer cancels stream -> employee gets 3,500 tUSD8, employer gets 4,000 tUSD8
        vm.warp(startAt + 60 days);
        uint256 employerBefore = token.balanceOf(payer);
        vm.prank(payer);
        (uint256 empOut, uint256 empRefund) = vault.cancelStream(streamId);
        assertEq(empOut, 3_500 * AevumConstants.TUSD8_UNIT);
        assertEq(empRefund, 4_000 * AevumConstants.TUSD8_UNIT);
        assertEq(token.balanceOf(employee), 6_000 * AevumConstants.TUSD8_UNIT);
        assertEq(token.balanceOf(payer) - employerBefore, 4_000 * AevumConstants.TUSD8_UNIT);
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function test_FeeOnTransferTokenRejected() public {
        FeeOnTransferMockToken fot = new FeeOnTransferMockToken();
        AevumInvoiceVault fotVault = new AevumInvoiceVault(address(fot), address(registry));
        fot.mint(payer, 1_000 * AevumConstants.TUSD8_UNIT);
        vm.prank(payer);
        fot.approve(address(fotVault), type(uint256).max);

        uint256[] memory tranches = new uint256[](1);
        tranches[0] = 100 * AevumConstants.TUSD8_UNIT;

        vm.prank(payer);
        vm.expectRevert(
            abi.encodeWithSelector(
                AevumInvoiceVault.ExactTransferAmountMismatch.selector,
                100 * AevumConstants.TUSD8_UNIT,
                99 * AevumConstants.TUSD8_UNIT
            )
        );
        fotVault.createMilestoneEscrow(
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            arbiter,
            tranches,
            uint64(block.timestamp + 7 days),
            keccak256("FOT-REJECT")
        );
    }

    function test_ReentrancyAttackBlockedOnStreamClaim() public {
        ReentrantPayeeToken evilToken = new ReentrantPayeeToken();
        AevumInvoiceVault evilVault = new AevumInvoiceVault(address(evilToken), address(registry));
        evilToken.mint(payer, 10_000 * AevumConstants.TUSD8_UNIT);
        vm.prank(payer);
        evilToken.approve(address(evilVault), type(uint256).max);

        uint64 startAt = uint64(block.timestamp);
        vm.prank(payer);
        uint256 streamId = evilVault.createPayrollStream(
            employee,
            EMPLOYEE_HANDLE,
            EMPLOYEE_LEGAL,
            1_000 * AevumConstants.TUSD8_UNIT,
            startAt,
            startAt,
            startAt + 100,
            true
        );

        vm.warp(startAt + 50);
        evilToken.arm(evilVault, streamId);

        vm.prank(employee);
        vm.expectRevert(
            abi.encodeWithSelector(
                SafeERC20Transfer.SafeTransferFailed.selector,
                address(evilToken),
                employee,
                500 * AevumConstants.TUSD8_UNIT
            )
        );
        evilVault.claimStream(streamId);
    }

    function testFuzz_DisputeSplitZeroDust(uint96 rawAmount, uint16 refundBps) public {
        uint256 total = bound(uint256(rawAmount), 1, 100_000 * AevumConstants.TUSD8_UNIT);
        uint16 bps = uint16(bound(uint256(refundBps), 0, AevumConstants.BPS_DENOMINATOR));

        uint256[] memory tranches = new uint256[](1);
        tranches[0] = total;

        vm.prank(payer);
        uint256 escId = vault.createMilestoneEscrow(
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            arbiter,
            tranches,
            uint64(block.timestamp + 14 days),
            keccak256("FUZZ-DISPUTE")
        );

        vm.prank(payer);
        vault.raiseDispute(escId, keccak256("fuzz-reason"));

        uint256 payerBefore = token.balanceOf(payer);
        uint256 vendorBefore = token.balanceOf(vendor);

        vm.prank(arbiter);
        vault.resolveDispute(escId, bps);

        uint256 payerGain = token.balanceOf(payer) - payerBefore;
        uint256 vendorGain = token.balanceOf(vendor) - vendorBefore;
        assertEq(payerGain + vendorGain, total);
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function testFuzz_EarlyDiscountSettlementConservation(
        uint96 rawPrincipal,
        uint16 discountBps
    ) public {
        uint256 principal = bound(uint256(rawPrincipal), 1, 100_000 * AevumConstants.TUSD8_UNIT);
        uint16 bps = uint16(bound(uint256(discountBps), 0, AevumConstants.MAX_DISCOUNT_BPS));

        vm.prank(vendor);
        uint256 invId = vault.createInvoice(
            payer,
            VENDOR_HANDLE,
            principal,
            bps,
            uint64(block.timestamp + 1 days),
            uint64(block.timestamp + 7 days),
            keccak256("FUZZ-INV")
        );

        uint256 payerBefore = token.balanceOf(payer);
        uint256 vendorBefore = token.balanceOf(vendor);

        vm.prank(payer);
        (uint256 settled,) = vault.payVerifiedInvoice(
            invId,
            vendor,
            VENDOR_HANDLE,
            VENDOR_LEGAL,
            principal
        );

        uint256 expectedDiscount = (principal * bps) / AevumConstants.BPS_DENOMINATOR;
        assertEq(settled, principal - expectedDiscount);
        assertEq(payerBefore - token.balanceOf(payer), settled);
        assertEq(token.balanceOf(vendor) - vendorBefore, settled);
        assertEq(token.balanceOf(address(vault)), 0);
    }

    function testFuzz_PayrollStreamExactConservation(
        uint96 rawDeposit,
        uint32 duration,
        uint32 cancelOffset
    ) public {
        uint256 deposit = bound(uint256(rawDeposit), 1, 100_000 * AevumConstants.TUSD8_UNIT);
        uint64 dur = uint64(bound(uint256(duration), 10, 365 days));
        uint64 offset = uint64(bound(uint256(cancelOffset), 0, dur + 30 days));
        uint64 startAt = uint64(block.timestamp);
        uint64 endAt = startAt + dur;

        vm.prank(payer);
        uint256 streamId = vault.createPayrollStream(
            employee,
            EMPLOYEE_HANDLE,
            EMPLOYEE_LEGAL,
            deposit,
            startAt,
            startAt,
            endAt,
            true
        );

        vm.warp(startAt + offset);
        uint256 payerBefore = token.balanceOf(payer);
        uint256 empBefore = token.balanceOf(employee);

        vm.prank(payer);
        (uint256 empPayout, uint256 empRefund) = vault.cancelStream(streamId);

        assertEq(empPayout + empRefund, deposit);
        assertEq(token.balanceOf(employee) - empBefore, empPayout);
        assertEq(token.balanceOf(payer) - payerBefore, empRefund);
        assertEq(token.balanceOf(address(vault)), 0);
    }
}
