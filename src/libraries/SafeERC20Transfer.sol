// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20Metadata} from "../interfaces/IERC20Metadata.sol";

library SafeERC20Transfer {
    error TokenNotContract(address token);
    error SafeTransferFailed(address token, address to, uint256 amount);
    error SafeTransferFromFailed(address token, address from, address to, uint256 amount);

    function safeTransfer(IERC20Metadata token, address to, uint256 amount) internal {
        address tokenAddr = address(token);
        if (tokenAddr.code.length == 0) revert TokenNotContract(tokenAddr);
        (bool ok, bytes memory ret) =
            tokenAddr.call(abi.encodeWithSelector(IERC20Metadata.transfer.selector, to, amount));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) {
            revert SafeTransferFailed(tokenAddr, to, amount);
        }
    }

    function safeTransferFrom(IERC20Metadata token, address from, address to, uint256 amount) internal {
        address tokenAddr = address(token);
        if (tokenAddr.code.length == 0) revert TokenNotContract(tokenAddr);
        (bool ok, bytes memory ret) =
            tokenAddr.call(abi.encodeWithSelector(IERC20Metadata.transferFrom.selector, from, to, amount));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) {
            revert SafeTransferFromFailed(tokenAddr, from, to, amount);
        }
    }
}
