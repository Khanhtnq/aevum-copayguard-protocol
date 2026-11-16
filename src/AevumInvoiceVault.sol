// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20Metadata} from "./interfaces/IERC20Metadata.sol";
import {AevumConstants} from "./libraries/AevumConstants.sol";
import {ReentrancyGuard} from "./libraries/ReentrancyGuard.sol";
import {SafeERC20Transfer} from "./libraries/SafeERC20Transfer.sol";
import {AevumPayeeRegistry} from "./AevumPayeeRegistry.sol";

/// @title AevumInvoiceVault
/// @notice Programmable 6-decimal tUSD8 settlement engine on QDay Aevum Testnet (Chain ID 44005)
///         unifying Confirmation-of-Payee invoices, milestone escrows, and linear payroll streams.
contract AevumInvoiceVault is ReentrancyGuard {
    using SafeERC20Transfer for IERC20Metadata;

    error ZeroAddress();
    error UnsupportedTokenDecimals(uint8 actualDecimals, uint8 expectedDecimals);
    error ExactTransferAmountMismatch(uint256 expectedAmount, uint256 receivedAmount);
    error InvalidAmount();
    error InvalidDiscountBps(uint16 discountBps);
    error InvalidSchedule();
    error InvalidInvoiceState(uint256 invoiceId, InvoiceStatus status);
    error UnauthorizedPayer(address caller, address designatedPayer);
    error UnauthorizedCaller(address caller);
    error InvoiceExpired(uint256 invoiceId, uint64 dueAt, uint256 currentTimestamp);
    error SlippageOrTermsExceeded(uint256 settledAmount, uint256 maxSettlementAmount);
    error PayeeHandleMismatch(bytes32 expectedHandleHash, bytes32 actualHandleHash);
    error InvalidMilestonesCount(uint256 count);
    error InvalidEscrowState(uint256 escrowId, EscrowStatus status);
    error MilestoneAlreadyReleased(uint256 escrowId, uint256 trancheIndex);
    error InvalidTrancheIndex(uint256 trancheIndex, uint256 totalTranches);
    error EscrowNotExpired(uint256 escrowId, uint64 expiresAt);
    error InvalidRefundBps(uint16 refundBps);
    error NothingClaimable(uint256 streamId);
    error StreamNotActive(uint256 streamId);
    error StreamNotCancelable(uint256 streamId);

    enum InvoiceStatus {
        None,
        Open,
        Settled,
        Canceled
    }

    enum EscrowStatus {
        None,
        Funded,
        Completed,
        Disputed,
        Resolved,
        Refunded
    }

    struct Invoice {
        address issuer;
        address designatedPayer;
        bytes32 payeeHandleHash;
        bytes32 invoiceRefHash;
        uint256 principalAmount;
        uint256 settledAmount;
        uint16 earlyDiscountBps;
        uint64 discountDeadline;
        uint64 dueAt;
        uint64 settledAt;
        address settledBy;
        bytes32 receiptDigest;
        InvoiceStatus status;
    }

    struct MilestoneEscrow {
        address payer;
        address payee;
        address arbiter;
        bytes32 payeeHandleHash;
        bytes32 contractRefHash;
        uint256 totalAmount;
        uint256 releasedAmount;
        uint64 expiresAt;
        EscrowStatus status;
    }

    struct PayrollStream {
        address employer;
        address recipient;
        bytes32 payeeHandleHash;
        uint256 depositAmount;
        uint256 withdrawnAmount;
        uint64 startAt;
        uint64 cliffAt;
        uint64 endAt;
        bool cancelable;
        bool active;
    }

    IERC20Metadata public immutable tusd8;
    AevumPayeeRegistry public immutable payeeRegistry;
    uint8 public immutable tokenDecimals;

    uint256 public nextInvoiceId = 1;
    uint256 public nextEscrowId = 1;
    uint256 public nextStreamId = 1;

    mapping(uint256 => Invoice) private _invoices;
    mapping(uint256 => MilestoneEscrow) private _escrows;
    mapping(uint256 => uint256[]) private _escrowTranches;
    mapping(uint256 => bool[]) private _escrowTrancheReleased;
    mapping(uint256 => PayrollStream) private _streams;

    event InvoiceCreated(
        uint256 indexed invoiceId,
        address indexed issuer,
        address indexed designatedPayer,
        bytes32 payeeHandleHash,
        uint256 principalAmount,
        uint16 earlyDiscountBps,
        uint64 discountDeadline,
        uint64 dueAt,
        bytes32 invoiceRefHash
    );
    event InvoiceCanceled(uint256 indexed invoiceId, address indexed issuer);
    event InvoiceSettled(
        uint256 indexed invoiceId,
        address indexed payer,
        address indexed payee,
        bytes32 payeeHandleHash,
        uint256 settledAmount,
        uint256 discountSaved,
        bytes32 receiptDigest
    );

    event MilestoneEscrowCreated(
        uint256 indexed escrowId,
        address indexed payer,
        address indexed payee,
        bytes32 payeeHandleHash,
        address arbiter,
        uint256 totalAmount,
        uint256 trancheCount,
        uint64 expiresAt
    );
    event MilestoneReleased(
        uint256 indexed escrowId,
        uint256 indexed trancheIndex,
        address indexed payee,
        uint256 amount
    );
    event EscrowDisputed(uint256 indexed escrowId, address indexed initiator, bytes32 reasonHash);
    event EscrowDisputeResolved(
        uint256 indexed escrowId,
        address indexed arbiter,
        uint256 payerRefund,
        uint256 payeePayout
    );
    event EscrowExpiredReclaimed(
        uint256 indexed escrowId,
        address indexed payer,
        uint256 refundedAmount
    );

    event PayrollStreamCreated(
        uint256 indexed streamId,
        address indexed employer,
        address indexed recipient,
        bytes32 payeeHandleHash,
        uint256 depositAmount,
        uint64 startAt,
        uint64 cliffAt,
        uint64 endAt,
        bool cancelable
    );
    event PayrollStreamClaimed(
        uint256 indexed streamId,
        address indexed recipient,
        uint256 claimedAmount
    );
    event PayrollStreamCanceled(
        uint256 indexed streamId,
        address indexed caller,
        uint256 recipientPayout,
        uint256 employerRefund
    );

    constructor(address tokenAddress, address registryAddress) {
        if (tokenAddress == address(0) || registryAddress == address(0)) revert ZeroAddress();
        IERC20Metadata token = IERC20Metadata(tokenAddress);
        uint8 dec = token.decimals();
        if (dec != AevumConstants.TUSD8_DECIMALS) {
            revert UnsupportedTokenDecimals(dec, AevumConstants.TUSD8_DECIMALS);
        }
        tusd8 = token;
        payeeRegistry = AevumPayeeRegistry(registryAddress);
        tokenDecimals = dec;
    }

    // -------------------------------------------------------------------------
    // 1. Confirmation-of-Payee Structured Invoices
    // -------------------------------------------------------------------------

    function createInvoice(
        address designatedPayer,
        bytes32 payeeHandleHash,
        uint256 principalAmount,
        uint16 earlyDiscountBps,
        uint64 discountDeadline,
        uint64 dueAt,
        bytes32 invoiceRefHash
    ) external returns (uint256 invoiceId) {
        if (principalAmount == 0) revert InvalidAmount();
        if (earlyDiscountBps > AevumConstants.MAX_DISCOUNT_BPS) {
            revert InvalidDiscountBps(earlyDiscountBps);
        }
        if (dueAt <= block.timestamp) revert InvalidSchedule();
        if (earlyDiscountBps > 0 && (discountDeadline == 0 || discountDeadline > dueAt)) {
            revert InvalidSchedule();
        }

        payeeRegistry.requireVerifiedPayee(msg.sender, payeeHandleHash, bytes32(0));

        invoiceId = nextInvoiceId++;
        _invoices[invoiceId] = Invoice({
            issuer: msg.sender,
            designatedPayer: designatedPayer,
            payeeHandleHash: payeeHandleHash,
            invoiceRefHash: invoiceRefHash,
            principalAmount: principalAmount,
            settledAmount: 0,
            earlyDiscountBps: earlyDiscountBps,
            discountDeadline: discountDeadline,
            dueAt: dueAt,
            settledAt: 0,
            settledBy: address(0),
            receiptDigest: bytes32(0),
            status: InvoiceStatus.Open
        });

        emit InvoiceCreated(
            invoiceId,
            msg.sender,
            designatedPayer,
            payeeHandleHash,
            principalAmount,
            earlyDiscountBps,
            discountDeadline,
            dueAt,
            invoiceRefHash
        );
    }

    function cancelInvoice(uint256 invoiceId) external {
        Invoice storage inv = _invoices[invoiceId];
        if (inv.status != InvoiceStatus.Open) revert InvalidInvoiceState(invoiceId, inv.status);
        if (msg.sender != inv.issuer) revert UnauthorizedCaller(msg.sender);

        inv.status = InvoiceStatus.Canceled;
        emit InvoiceCanceled(invoiceId, msg.sender);
    }

    function quoteInvoiceSettlement(
        uint256 invoiceId,
        uint256 atTimestamp
    ) public view returns (uint256 payableAmount, uint256 discountSaved) {
        Invoice storage inv = _invoices[invoiceId];
        if (inv.status != InvoiceStatus.Open) revert InvalidInvoiceState(invoiceId, inv.status);
        if (inv.earlyDiscountBps > 0 && atTimestamp <= inv.discountDeadline) {
            discountSaved =
                (inv.principalAmount * inv.earlyDiscountBps) / AevumConstants.BPS_DENOMINATOR;
            payableAmount = inv.principalAmount - discountSaved;
        } else {
            discountSaved = 0;
            payableAmount = inv.principalAmount;
        }
    }

    function payVerifiedInvoice(
        uint256 invoiceId,
        address expectedPayeeWallet,
        bytes32 expectedHandleHash,
        bytes32 expectedLegalNameHash,
        uint256 maxSettlementAmount
    ) external nonReentrant returns (uint256 settledAmount, bytes32 receiptDigest) {
        Invoice storage inv = _invoices[invoiceId];
        if (inv.status != InvoiceStatus.Open) revert InvalidInvoiceState(invoiceId, inv.status);
        if (block.timestamp > inv.dueAt) {
            revert InvoiceExpired(invoiceId, inv.dueAt, block.timestamp);
        }
        if (inv.designatedPayer != address(0) && inv.designatedPayer != msg.sender) {
            revert UnauthorizedPayer(msg.sender, inv.designatedPayer);
        }
        if (expectedHandleHash != inv.payeeHandleHash) {
            revert PayeeHandleMismatch(expectedHandleHash, inv.payeeHandleHash);
        }
        if (expectedPayeeWallet != inv.issuer) {
            revert AevumPayeeRegistry.PayeeWalletMismatch(
                expectedHandleHash,
                expectedPayeeWallet,
                inv.issuer
            );
        }

        payeeRegistry.requireVerifiedPayee(
            expectedPayeeWallet,
            expectedHandleHash,
            expectedLegalNameHash
        );

        uint256 discountSaved;
        (settledAmount, discountSaved) = quoteInvoiceSettlement(invoiceId, block.timestamp);
        if (settledAmount > maxSettlementAmount) {
            revert SlippageOrTermsExceeded(settledAmount, maxSettlementAmount);
        }

        receiptDigest = keccak256(
            abi.encode(
                block.chainid,
                address(this),
                invoiceId,
                msg.sender,
                expectedPayeeWallet,
                settledAmount,
                inv.invoiceRefHash,
                block.timestamp
            )
        );

        inv.status = InvoiceStatus.Settled;
        inv.settledAmount = settledAmount;
        inv.settledAt = uint64(block.timestamp);
        inv.settledBy = msg.sender;
        inv.receiptDigest = receiptDigest;

        _pullExact(msg.sender, settledAmount);
        tusd8.safeTransfer(expectedPayeeWallet, settledAmount);

        emit InvoiceSettled(
            invoiceId,
            msg.sender,
            expectedPayeeWallet,
            expectedHandleHash,
            settledAmount,
            discountSaved,
            receiptDigest
        );
    }

    function getInvoice(uint256 invoiceId) external view returns (Invoice memory) {
        return _invoices[invoiceId];
    }

    // -------------------------------------------------------------------------
    // 2. Funded Milestone Escrow with Arbiter Dispute Resolution
    // -------------------------------------------------------------------------

    function createMilestoneEscrow(
        address payee,
        bytes32 payeeHandleHash,
        bytes32 expectedLegalNameHash,
        address arbiter,
        uint256[] calldata trancheAmounts,
        uint64 expiresAt,
        bytes32 contractRefHash
    ) external nonReentrant returns (uint256 escrowId) {
        if (payee == address(0) || arbiter == address(0)) revert ZeroAddress();
        uint256 count = trancheAmounts.length;
        if (count == 0 || count > AevumConstants.MAX_MILESTONES) {
            revert InvalidMilestonesCount(count);
        }
        if (expiresAt <= block.timestamp) revert InvalidSchedule();

        payeeRegistry.requireVerifiedPayee(payee, payeeHandleHash, expectedLegalNameHash);

        uint256 totalAmount = 0;
        for (uint256 i = 0; i < count; ++i) {
            if (trancheAmounts[i] == 0) revert InvalidAmount();
            totalAmount += trancheAmounts[i];
        }

        escrowId = nextEscrowId++;
        _escrows[escrowId] = MilestoneEscrow({
            payer: msg.sender,
            payee: payee,
            arbiter: arbiter,
            payeeHandleHash: payeeHandleHash,
            contractRefHash: contractRefHash,
            totalAmount: totalAmount,
            releasedAmount: 0,
            expiresAt: expiresAt,
            status: EscrowStatus.Funded
        });

        uint256[] storage storedTranches = _escrowTranches[escrowId];
        bool[] storage releasedFlags = _escrowTrancheReleased[escrowId];
        for (uint256 i = 0; i < count; ++i) {
            storedTranches.push(trancheAmounts[i]);
            releasedFlags.push(false);
        }

        _pullExact(msg.sender, totalAmount);

        emit MilestoneEscrowCreated(
            escrowId,
            msg.sender,
            payee,
            payeeHandleHash,
            arbiter,
            totalAmount,
            count,
            expiresAt
        );
    }

    function releaseMilestone(uint256 escrowId, uint256 trancheIndex) external nonReentrant {
        MilestoneEscrow storage esc = _escrows[escrowId];
        if (esc.status != EscrowStatus.Funded) revert InvalidEscrowState(escrowId, esc.status);
        if (msg.sender != esc.payer) revert UnauthorizedCaller(msg.sender);

        uint256[] storage tranches = _escrowTranches[escrowId];
        if (trancheIndex >= tranches.length) {
            revert InvalidTrancheIndex(trancheIndex, tranches.length);
        }
        if (_escrowTrancheReleased[escrowId][trancheIndex]) {
            revert MilestoneAlreadyReleased(escrowId, trancheIndex);
        }

        payeeRegistry.requireVerifiedPayee(esc.payee, esc.payeeHandleHash, bytes32(0));

        uint256 amount = tranches[trancheIndex];
        _escrowTrancheReleased[escrowId][trancheIndex] = true;
        esc.releasedAmount += amount;
        if (esc.releasedAmount == esc.totalAmount) {
            esc.status = EscrowStatus.Completed;
        }

        tusd8.safeTransfer(esc.payee, amount);
        emit MilestoneReleased(escrowId, trancheIndex, esc.payee, amount);
    }

    function raiseDispute(uint256 escrowId, bytes32 reasonHash) external {
        MilestoneEscrow storage esc = _escrows[escrowId];
        if (esc.status != EscrowStatus.Funded) revert InvalidEscrowState(escrowId, esc.status);
        if (msg.sender != esc.payer && msg.sender != esc.payee) {
            revert UnauthorizedCaller(msg.sender);
        }
        esc.status = EscrowStatus.Disputed;
        emit EscrowDisputed(escrowId, msg.sender, reasonHash);
    }

    function resolveDispute(
        uint256 escrowId,
        uint16 payerRefundBps
    ) external nonReentrant {
        MilestoneEscrow storage esc = _escrows[escrowId];
        if (esc.status != EscrowStatus.Disputed) revert InvalidEscrowState(escrowId, esc.status);
        if (msg.sender != esc.arbiter) revert UnauthorizedCaller(msg.sender);
        if (payerRefundBps > AevumConstants.BPS_DENOMINATOR) {
            revert InvalidRefundBps(payerRefundBps);
        }

        uint256 remaining = esc.totalAmount - esc.releasedAmount;
        uint256 payerRefund = (remaining * payerRefundBps) / AevumConstants.BPS_DENOMINATOR;
        uint256 payeePayout = remaining - payerRefund;

        esc.releasedAmount = esc.totalAmount;
        esc.status = EscrowStatus.Resolved;

        if (payerRefund > 0) {
            tusd8.safeTransfer(esc.payer, payerRefund);
        }
        if (payeePayout > 0) {
            tusd8.safeTransfer(esc.payee, payeePayout);
        }

        emit EscrowDisputeResolved(escrowId, msg.sender, payerRefund, payeePayout);
    }

    function reclaimExpiredEscrow(uint256 escrowId) external nonReentrant {
        MilestoneEscrow storage esc = _escrows[escrowId];
        if (esc.status != EscrowStatus.Funded) revert InvalidEscrowState(escrowId, esc.status);
        if (msg.sender != esc.payer) revert UnauthorizedCaller(msg.sender);
        if (block.timestamp <= esc.expiresAt) {
            revert EscrowNotExpired(escrowId, esc.expiresAt);
        }

        uint256 remaining = esc.totalAmount - esc.releasedAmount;
        esc.releasedAmount = esc.totalAmount;
        esc.status = EscrowStatus.Refunded;

        tusd8.safeTransfer(esc.payer, remaining);
        emit EscrowExpiredReclaimed(escrowId, msg.sender, remaining);
    }

    function getEscrow(
        uint256 escrowId
    )
        external
        view
        returns (
            MilestoneEscrow memory escrow,
            uint256[] memory trancheAmounts,
            bool[] memory releasedFlags
        )
    {
        return (_escrows[escrowId], _escrowTranches[escrowId], _escrowTrancheReleased[escrowId]);
    }

    // -------------------------------------------------------------------------
    // 3. Linear tUSD8 Payroll & Token Vesting Streams
    // -------------------------------------------------------------------------

    function createPayrollStream(
        address recipient,
        bytes32 payeeHandleHash,
        bytes32 expectedLegalNameHash,
        uint256 depositAmount,
        uint64 startAt,
        uint64 cliffAt,
        uint64 endAt,
        bool cancelable
    ) external nonReentrant returns (uint256 streamId) {
        if (recipient == address(0)) revert ZeroAddress();
        if (depositAmount == 0) revert InvalidAmount();
        if (endAt <= startAt || cliffAt < startAt || cliffAt > endAt) {
            revert InvalidSchedule();
        }

        payeeRegistry.requireVerifiedPayee(recipient, payeeHandleHash, expectedLegalNameHash);

        streamId = nextStreamId++;
        _streams[streamId] = PayrollStream({
            employer: msg.sender,
            recipient: recipient,
            payeeHandleHash: payeeHandleHash,
            depositAmount: depositAmount,
            withdrawnAmount: 0,
            startAt: startAt,
            cliffAt: cliffAt,
            endAt: endAt,
            cancelable: cancelable,
            active: true
        });

        _pullExact(msg.sender, depositAmount);

        emit PayrollStreamCreated(
            streamId,
            msg.sender,
            recipient,
            payeeHandleHash,
            depositAmount,
            startAt,
            cliffAt,
            endAt,
            cancelable
        );
    }

    function vestedAmount(uint256 streamId, uint256 atTimestamp) public view returns (uint256) {
        PayrollStream storage s = _streams[streamId];
        if (s.depositAmount == 0 || atTimestamp < s.cliffAt) return 0;
        if (atTimestamp >= s.endAt) return s.depositAmount;
        return (s.depositAmount * (atTimestamp - s.startAt)) / (s.endAt - s.startAt);
    }

    function claimableStream(uint256 streamId) public view returns (uint256) {
        PayrollStream storage s = _streams[streamId];
        if (!s.active) return 0;
        uint256 vested = vestedAmount(streamId, block.timestamp);
        if (vested <= s.withdrawnAmount) return 0;
        return vested - s.withdrawnAmount;
    }

    function claimStream(uint256 streamId) external nonReentrant returns (uint256 claimedAmount) {
        PayrollStream storage s = _streams[streamId];
        if (!s.active) revert StreamNotActive(streamId);
        if (msg.sender != s.recipient) revert UnauthorizedCaller(msg.sender);

        claimedAmount = claimableStream(streamId);
        if (claimedAmount == 0) revert NothingClaimable(streamId);

        s.withdrawnAmount += claimedAmount;
        if (s.withdrawnAmount == s.depositAmount) {
            s.active = false;
        }

        tusd8.safeTransfer(s.recipient, claimedAmount);
        emit PayrollStreamClaimed(streamId, s.recipient, claimedAmount);
    }

    function cancelStream(
        uint256 streamId
    ) external nonReentrant returns (uint256 recipientPayout, uint256 employerRefund) {
        PayrollStream storage s = _streams[streamId];
        if (!s.active) revert StreamNotActive(streamId);
        if (!s.cancelable) revert StreamNotCancelable(streamId);
        if (msg.sender != s.employer && msg.sender != s.recipient) {
            revert UnauthorizedCaller(msg.sender);
        }

        uint256 vested = vestedAmount(streamId, block.timestamp);
        recipientPayout = vested > s.withdrawnAmount ? vested - s.withdrawnAmount : 0;
        employerRefund = s.depositAmount - vested;

        s.withdrawnAmount = vested;
        s.active = false;

        if (recipientPayout > 0) {
            tusd8.safeTransfer(s.recipient, recipientPayout);
        }
        if (employerRefund > 0) {
            tusd8.safeTransfer(s.employer, employerRefund);
        }

        emit PayrollStreamCanceled(streamId, msg.sender, recipientPayout, employerRefund);
    }

    function getStream(uint256 streamId) external view returns (PayrollStream memory) {
        return _streams[streamId];
    }

    // -------------------------------------------------------------------------
    // Internal Exact-Accounting Pull
    // -------------------------------------------------------------------------

    function _pullExact(address from, uint256 amount) internal {
        uint256 beforeBal = tusd8.balanceOf(address(this));
        tusd8.safeTransferFrom(from, address(this), amount);
        uint256 afterBal = tusd8.balanceOf(address(this));
        uint256 received = afterBal - beforeBal;
        if (received != amount) {
            revert ExactTransferAmountMismatch(amount, received);
        }
    }
}
