// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title AevumPayeeRegistry
/// @notice On-chain Confirmation-of-Payee (CoP) business directory protecting Aevum tUSD8 settlements
///         against lookalike-address poisoning, unauthorized wallet swaps, and recipient spoofing.
contract AevumPayeeRegistry {
    error InvalidHandleHash();
    error InvalidLegalNameHash();
    error InvalidWalletAddress();
    error HandleAlreadyRegistered(bytes32 handleHash);
    error PayeeNotRegistered(bytes32 handleHash);
    error UnauthorizedPayeeOwner(bytes32 handleHash, address caller);
    error NoPendingWalletRotation(bytes32 handleHash);
    error UnauthorizedPendingWallet(bytes32 handleHash, address caller);
    error PayeeInactive(bytes32 handleHash);
    error PayeeWalletMismatch(bytes32 handleHash, address expectedWallet, address actualWallet);
    error PayeeLegalNameMismatch(bytes32 handleHash, bytes32 expectedLegalNameHash, bytes32 actualLegalNameHash);

    struct PayeeProfile {
        address wallet;
        address pendingWallet;
        bytes32 legalNameHash;
        bytes32 metadataHash;
        uint64 registeredAt;
        uint64 updatedAt;
        bool active;
    }

    mapping(bytes32 => PayeeProfile) private _profiles;
    mapping(address => bytes32) public primaryHandleOf;

    event PayeeRegistered(
        bytes32 indexed handleHash,
        address indexed wallet,
        bytes32 indexed legalNameHash,
        bytes32 metadataHash
    );
    event WalletRotationProposed(
        bytes32 indexed handleHash,
        address indexed currentWallet,
        address indexed pendingWallet
    );
    event WalletRotationAccepted(
        bytes32 indexed handleHash,
        address indexed previousWallet,
        address indexed newWallet
    );
    event PayeeMetadataUpdated(bytes32 indexed handleHash, bytes32 metadataHash);
    event PayeeStatusUpdated(bytes32 indexed handleHash, bool active);

    function registerPayee(
        bytes32 handleHash,
        bytes32 legalNameHash,
        bytes32 metadataHash
    ) external {
        if (handleHash == bytes32(0)) revert InvalidHandleHash();
        if (legalNameHash == bytes32(0)) revert InvalidLegalNameHash();
        if (_profiles[handleHash].registeredAt != 0) {
            revert HandleAlreadyRegistered(handleHash);
        }

        uint64 nowTs = uint64(block.timestamp);
        _profiles[handleHash] = PayeeProfile({
            wallet: msg.sender,
            pendingWallet: address(0),
            legalNameHash: legalNameHash,
            metadataHash: metadataHash,
            registeredAt: nowTs,
            updatedAt: nowTs,
            active: true
        });

        if (primaryHandleOf[msg.sender] == bytes32(0)) {
            primaryHandleOf[msg.sender] = handleHash;
        }

        emit PayeeRegistered(handleHash, msg.sender, legalNameHash, metadataHash);
    }

    function proposeWalletRotation(bytes32 handleHash, address newWallet) external {
        PayeeProfile storage profile = _requireOwner(handleHash);
        if (newWallet == address(0) || newWallet == profile.wallet) {
            revert InvalidWalletAddress();
        }
        profile.pendingWallet = newWallet;
        profile.updatedAt = uint64(block.timestamp);
        emit WalletRotationProposed(handleHash, profile.wallet, newWallet);
    }

    function acceptWalletRotation(bytes32 handleHash) external {
        PayeeProfile storage profile = _profiles[handleHash];
        if (profile.registeredAt == 0) revert PayeeNotRegistered(handleHash);
        address pending = profile.pendingWallet;
        if (pending == address(0)) revert NoPendingWalletRotation(handleHash);
        if (msg.sender != pending) revert UnauthorizedPendingWallet(handleHash, msg.sender);

        address previous = profile.wallet;
        if (primaryHandleOf[previous] == handleHash) {
            delete primaryHandleOf[previous];
        }
        profile.wallet = pending;
        profile.pendingWallet = address(0);
        profile.updatedAt = uint64(block.timestamp);
        if (primaryHandleOf[pending] == bytes32(0)) {
            primaryHandleOf[pending] = handleHash;
        }

        emit WalletRotationAccepted(handleHash, previous, pending);
    }

    function updateMetadata(bytes32 handleHash, bytes32 metadataHash) external {
        PayeeProfile storage profile = _requireOwner(handleHash);
        profile.metadataHash = metadataHash;
        profile.updatedAt = uint64(block.timestamp);
        emit PayeeMetadataUpdated(handleHash, metadataHash);
    }

    function setPayeeActive(bytes32 handleHash, bool active) external {
        PayeeProfile storage profile = _requireOwner(handleHash);
        profile.active = active;
        profile.updatedAt = uint64(block.timestamp);
        emit PayeeStatusUpdated(handleHash, active);
    }

    function getPayee(bytes32 handleHash) external view returns (PayeeProfile memory) {
        return _profiles[handleHash];
    }

    function verifyPayeeMatch(
        address expectedWallet,
        bytes32 expectedHandleHash,
        bytes32 expectedLegalNameHash
    ) external view returns (bool) {
        PayeeProfile storage profile = _profiles[expectedHandleHash];
        if (profile.registeredAt == 0 || !profile.active) return false;
        if (profile.wallet != expectedWallet) return false;
        if (
            expectedLegalNameHash != bytes32(0)
                && profile.legalNameHash != expectedLegalNameHash
        ) {
            return false;
        }
        return true;
    }

    function requireVerifiedPayee(
        address expectedWallet,
        bytes32 expectedHandleHash,
        bytes32 expectedLegalNameHash
    ) external view {
        PayeeProfile storage profile = _profiles[expectedHandleHash];
        if (profile.registeredAt == 0) revert PayeeNotRegistered(expectedHandleHash);
        if (!profile.active) revert PayeeInactive(expectedHandleHash);
        if (profile.wallet != expectedWallet) {
            revert PayeeWalletMismatch(expectedHandleHash, expectedWallet, profile.wallet);
        }
        if (
            expectedLegalNameHash != bytes32(0)
                && profile.legalNameHash != expectedLegalNameHash
        ) {
            revert PayeeLegalNameMismatch(
                expectedHandleHash,
                expectedLegalNameHash,
                profile.legalNameHash
            );
        }
    }

    function _requireOwner(bytes32 handleHash) internal view returns (PayeeProfile storage) {
        PayeeProfile storage profile = _profiles[handleHash];
        if (profile.registeredAt == 0) revert PayeeNotRegistered(handleHash);
        if (profile.wallet != msg.sender) {
            revert UnauthorizedPayeeOwner(handleHash, msg.sender);
        }
        return profile;
    }
}
