from typing import Callable, Dict, List

import pytest
from eth_abi import encode_single
from eth_tester.exceptions import TransactionFailed
from eth_typing import HexAddress
from eth_utils import to_checksum_address
from web3 import Web3
from web3.contract import Contract

from raiden_contracts.constants import (
    LOCKSROOT_OF_NO_LOCKS,
    TEST_SETTLE_TIMEOUT_MIN,
    MessageTypeId,
    MonitoringServiceEvent,
)
from raiden_contracts.tests.utils import SERVICE_DEPOSIT
from raiden_contracts.utils.proofs import sign_reward_proof

REWARD_AMOUNT = 10


@pytest.fixture
def ms_address(
    get_accounts: Callable, custom_token: Contract, service_registry: Contract
) -> HexAddress:
    (ms,) = get_accounts(1)

    # register MS in the ServiceRegistry contract
    custom_token.functions.mint(2 * SERVICE_DEPOSIT).call_and_transact({"from": ms})
    custom_token.functions.approve(service_registry.address, SERVICE_DEPOSIT).call_and_transact(
        {"from": ms}
    )
    service_registry.functions.deposit(SERVICE_DEPOSIT).call_and_transact({"from": ms})

    return ms


@pytest.fixture
def setup_monitor_data(
    get_accounts: Callable,
    deposit_to_udc: Callable,
    create_channel: Callable,
    create_balance_proof: Callable,
    create_balance_proof_countersignature: Callable,
    token_network: Contract,
    ms_address: HexAddress,
    get_private_key: Callable,
) -> Callable:
    def f(monitoring_service_contract: Contract) -> Dict:
        # Create two parties and a channel between them
        (A, B) = get_accounts(2, privkeys=["0x" + "1" * 64, "0x" + "2" * 64])
        deposit_to_udc(B, REWARD_AMOUNT)
        channel_identifier = create_channel(A, B)[0]

        # Create balance proofs
        balance_proof_A = create_balance_proof(
            channel_identifier, B, transferred_amount=10, nonce=1
        )
        balance_proof_B = create_balance_proof(
            channel_identifier, A, transferred_amount=20, nonce=2
        )

        # Add signatures by non_closing_participant
        closing_signature_A = create_balance_proof_countersignature(
            A, channel_identifier, MessageTypeId.BALANCE_PROOF, *balance_proof_A
        )
        non_closing_signature_B = create_balance_proof_countersignature(
            B, channel_identifier, MessageTypeId.BALANCE_PROOF_UPDATE, *balance_proof_B
        )
        reward_proof_signature = sign_reward_proof(
            privatekey=get_private_key(B),
            monitoring_service_contract_address=monitoring_service_contract.address,
            chain_id=token_network.functions.chain_id().call(),
            token_network_address=token_network.address,
            non_closing_participant=B,
            non_closing_signature=non_closing_signature_B,
            reward_amount=REWARD_AMOUNT,
        )

        # close channel
        token_network.functions.closeChannel(
            channel_identifier, B, A, *balance_proof_A, closing_signature_A
        ).call_and_transact({"from": A})

        # calculate when this MS is allowed to monitor
        (settle_block_number, _) = token_network.functions.getChannelInfo(
            channel_identifier, A, B
        ).call()
        first_allowed = monitoring_service_contract.functions.firstBlockAllowedToMonitor(
            closed_at_block=settle_block_number - TEST_SETTLE_TIMEOUT_MIN,
            settle_timeout=TEST_SETTLE_TIMEOUT_MIN,
            participant1=A,
            participant2=B,
            monitoring_service_address=ms_address,
        ).call()

        # return args for `monitor` function
        return {
            "participants": (A, B),
            "balance_proof_A": balance_proof_A,
            "balance_proof_B": balance_proof_B,
            "non_closing_signature": non_closing_signature_B,
            "reward_proof_signature": reward_proof_signature,
            "channel_identifier": channel_identifier,
            "first_allowed": first_allowed,
        }

    return f


@pytest.fixture
def monitor_data(setup_monitor_data: Callable, monitoring_service_external: Contract) -> Dict:
    return setup_monitor_data(monitoring_service_external)


@pytest.fixture
def monitor_data_internal(
    setup_monitor_data: Callable, monitoring_service_internals: Contract
) -> Dict:
    return setup_monitor_data(monitoring_service_internals)


@pytest.mark.parametrize("with_settle", [True, False])
def test_claimReward_with_settle_call(
    token_network: Contract,
    monitoring_service_external: Contract,
    user_deposit_contract: Contract,
    event_handler: Callable,
    monitor_data: Dict,
    ms_address: HexAddress,
    web3: Web3,
    with_settle: bool,
) -> None:
    A, B = monitor_data["participants"]
    channel_identifier = monitor_data["channel_identifier"]

    # wait until MS is allowed to monitor
    token_network.web3.testing.mine(monitor_data["first_allowed"] - web3.eth.blockNumber)

    # MS updates closed channel on behalf of B
    txn_hash = monitoring_service_external.functions.monitor(
        A,
        B,
        *monitor_data["balance_proof_B"],
        monitor_data["non_closing_signature"],
        REWARD_AMOUNT,
        token_network.address,
        monitor_data["reward_proof_signature"],
    ).call_and_transact({"from": ms_address})

    # claiming before settlement timeout must fail
    with pytest.raises(TransactionFailed, match="channel not settled yet"):
        monitoring_service_external.functions.claimReward(
            channel_identifier, token_network.address, A, B
        ).call({"from": ms_address})

    # Settle channel after settle_timeout elapsed
    token_network.web3.testing.mine(4)
    if with_settle:
        token_network.functions.settleChannel(
            channel_identifier,
            B,  # participant_B
            10,  # participant_B_transferred_amount
            0,  # participant_B_locked_amount
            LOCKSROOT_OF_NO_LOCKS,  # participant_B_locksroot
            A,  # participant_A
            20,  # participant_A_transferred_amount
            0,  # participant_A_locked_amount
            LOCKSROOT_OF_NO_LOCKS,  # participant_A_locksroot
        ).call_and_transact()

    # Claim reward for MS
    monitoring_service_external.functions.claimReward(
        channel_identifier, token_network.address, A, B
    ).call_and_transact({"from": ms_address})

    # Check REWARD_CLAIMED event
    reward_identifier = Web3.sha3(
        encode_single("uint256", channel_identifier) + Web3.toBytes(hexstr=token_network.address)
    )
    ms_ev_handler = event_handler(monitoring_service_external)
    ms_ev_handler.assert_event(
        txn_hash,
        MonitoringServiceEvent.REWARD_CLAIMED,
        dict(ms_address=ms_address, amount=REWARD_AMOUNT, reward_identifier=reward_identifier),
    )

    # Check that MS balance has increased by claiming the reward
    ms_balance_after_reward = user_deposit_contract.functions.balances(ms_address).call()
    assert ms_balance_after_reward == REWARD_AMOUNT


def test_monitor(
    token_network: Contract,
    monitoring_service_external: Contract,
    monitor_data: Dict,
    ms_address: HexAddress,
    event_handler: Callable,
    web3: Web3,
) -> None:
    A, B = monitor_data["participants"]

    # UpdateNonClosingBalanceProof is tested speparately, so we assume that all
    # parameters passed to it are handled correctly.

    # changing reward amount must lead to a failure during reward signature check
    with pytest.raises(TransactionFailed):
        txn_hash = monitoring_service_external.functions.monitor(
            A,
            B,
            *monitor_data["balance_proof_B"],
            monitor_data["non_closing_signature"],
            REWARD_AMOUNT + 1,
            token_network.address,
            monitor_data["reward_proof_signature"],
        ).call({"from": ms_address})

    # monitoring too early must fail
    with pytest.raises(TransactionFailed, match="not allowed to monitor"):
        assert web3.eth.blockNumber < monitor_data["first_allowed"]
        txn_hash = monitoring_service_external.functions.monitor(
            A,
            B,
            *monitor_data["balance_proof_B"],
            monitor_data["non_closing_signature"],
            REWARD_AMOUNT,
            token_network.address,
            monitor_data["reward_proof_signature"],
        ).call_and_transact({"from": ms_address})

    # wait until MS is allowed to monitor
    token_network.web3.testing.mine(monitor_data["first_allowed"] - web3.eth.blockNumber)

    # successful monitor call
    txn_hash = monitoring_service_external.functions.monitor(
        A,
        B,
        *monitor_data["balance_proof_B"],
        monitor_data["non_closing_signature"],
        REWARD_AMOUNT,
        token_network.address,
        monitor_data["reward_proof_signature"],
    ).call_and_transact({"from": ms_address})

    # NEW_BALANCE_PROOF_RECEIVED must get emitted
    ms_ev_handler = event_handler(monitoring_service_external)
    ms_ev_handler.assert_event(
        txn_hash,
        MonitoringServiceEvent.NEW_BALANCE_PROOF_RECEIVED,
        dict(
            token_network_address=token_network.address,
            channel_identifier=monitor_data["channel_identifier"],
            reward_amount=REWARD_AMOUNT,
            nonce=monitor_data["balance_proof_B"][1],
            ms_address=ms_address,
            raiden_node_address=B,
        ),
    )


def test_monitor_by_unregistered_service(
    token_network: Contract,
    monitoring_service_external: Contract,
    monitor_data: Dict,
    ms_address: HexAddress,
    web3: Web3,
) -> None:
    A, B = monitor_data["participants"]

    # wait until MS is allowed to monitor
    token_network.web3.testing.mine(monitor_data["first_allowed"] - web3.eth.blockNumber)

    # only registered service provicers may call `monitor`
    with pytest.raises(TransactionFailed, match="service not registered"):
        monitoring_service_external.functions.monitor(
            A,
            B,
            *monitor_data["balance_proof_B"],
            monitor_data["non_closing_signature"],
            REWARD_AMOUNT,
            token_network.address,
            monitor_data["reward_proof_signature"],
        ).call({"from": B})

    # See a success to make sure the above failure is not spurious
    monitoring_service_external.functions.monitor(
        A,
        B,
        *monitor_data["balance_proof_B"],
        monitor_data["non_closing_signature"],
        REWARD_AMOUNT,
        token_network.address,
        monitor_data["reward_proof_signature"],
    ).call_and_transact({"from": ms_address})


def test_monitor_on_wrong_token_network_registry(
    token_network_in_another_token_network_registry: Contract,
    monitoring_service_external: Contract,
    monitor_data: Dict,
    ms_address: HexAddress,
    web3: Web3,
) -> None:
    A, B = monitor_data["participants"]

    # wait until MS is allowed to monitor
    token_network_in_another_token_network_registry.web3.testing.mine(
        monitor_data["first_allowed"] - web3.eth.blockNumber
    )

    # monitor() call fails because the TokenNetwork is not registered on the
    # supposed TokenNetworkRegistry
    with pytest.raises(TransactionFailed, match="Unknown TokenNetwork"):
        monitoring_service_external.functions.monitor(
            A,
            B,
            *monitor_data["balance_proof_B"],
            monitor_data["non_closing_signature"],
            REWARD_AMOUNT,
            token_network_in_another_token_network_registry.address,
            monitor_data["reward_proof_signature"],
        ).call_and_transact({"from": ms_address})


def test_updateReward(
    monitoring_service_internals: Contract,
    ms_address: HexAddress,
    token_network: Contract,
    monitor_data_internal: Dict,
) -> None:
    A, B = monitor_data_internal["participants"]
    reward_identifier = Web3.sha3(
        encode_single("uint256", monitor_data_internal["channel_identifier"])
        + Web3.toBytes(hexstr=token_network.address)
    )

    def update_with_nonce(nonce: int) -> None:
        monitoring_service_internals.functions.updateRewardPublic(
            token_network.address,
            A,
            B,
            REWARD_AMOUNT,
            nonce,
            ms_address,
            monitor_data_internal["non_closing_signature"],
            monitor_data_internal["reward_proof_signature"],
        ).call_and_transact({"from": ms_address})

    # normal first call succeeds
    update_with_nonce(2)
    assert monitoring_service_internals.functions.rewardNonce(reward_identifier).call() == 2

    # calling again with same nonce fails
    with pytest.raises(TransactionFailed, match="stale nonce"):
        update_with_nonce(2)

    # calling again with higher nonce succeeds
    update_with_nonce(3)
    assert monitoring_service_internals.functions.rewardNonce(reward_identifier).call() == 3


def test_firstAllowedBlock(monitoring_service_external: Contract) -> None:
    def call(
        addresses: List[HexAddress], closed_at_block: int = 1000, settle_timeout: int = 100
    ) -> int:
        first_allowed = monitoring_service_external.functions.firstBlockAllowedToMonitor(
            closed_at_block=closed_at_block,
            settle_timeout=settle_timeout,
            participant1=to_checksum_address("0x%040x" % addresses[0]),
            participant2=to_checksum_address("0x%040x" % addresses[1]),
            monitoring_service_address=to_checksum_address("0x%040x" % addresses[2]),
        ).call()
        assert closed_at_block < first_allowed <= closed_at_block + settle_timeout
        return first_allowed

    # Basic example
    assert call([1, 2, 3]) == 1000 + 30 + (1 + 2 + 3)

    # Modulo used for one address
    assert call([100, 2, 3]) == 1000 + 30 + (100 % 50 + 2 + 3)

    # Modulo not used for any single address, but the sum of them
    assert call([40, 40, 40]) == 1000 + 30 + (40 + 40 + 40) % 50

    # Show that high address values don't cause overflows
    MAX_ADDRESS = 256 ** 20 - 1
    assert call([MAX_ADDRESS] * 3) == 1000 + 30 + (3 * MAX_ADDRESS) % 50

    # The highest settle_timeout does not cause overflows
    MAX_SETTLE_TIMEOUT = (2 ** 256 - 1) // 100 - 1
    assert call([1, 2, 3], settle_timeout=MAX_SETTLE_TIMEOUT) == 1000 + (
        MAX_SETTLE_TIMEOUT * 30
    ) // 100 + (1 + 2 + 3)

    # Extreme settle_timeout that would cause overflows will make the
    # transaction fail instead of giving the wrong result
    with pytest.raises(TransactionFailed, match="maliciously big settle timeout"):
        assert call([1, 2, 3], settle_timeout=MAX_SETTLE_TIMEOUT + 1)


def test_recoverAddressFromRewardProof(
    monitor_data_internal: Dict, token_network: Contract, monitoring_service_internals: Contract
) -> None:
    _, B = monitor_data_internal["participants"]

    recoverAddressFromRewardProof = (
        monitoring_service_internals.functions.recoverAddressFromRewardProofPublic
    )
    recovered_address = recoverAddressFromRewardProof(
        chain_id=token_network.functions.chain_id().call(),
        token_network_address=token_network.address,
        non_closing_participant=B,
        non_closing_signature=monitor_data_internal["non_closing_signature"],
        reward_amount=REWARD_AMOUNT,
        signature=monitor_data_internal["reward_proof_signature"],
    ).call()
    assert recovered_address == B
