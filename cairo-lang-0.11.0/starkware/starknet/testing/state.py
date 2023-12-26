import copy
from typing import List, MutableMapping, Optional, Tuple, Union

from starkware.cairo.lang.vm.crypto import pedersen_hash_func
from starkware.python.utils import from_bytes
from starkware.starknet.business_logic.execution.execute_entry_point import ExecuteEntryPoint
from starkware.starknet.business_logic.execution.objects import (
    CallInfo,
    Event,
    TransactionExecutionInfo,
)
from starkware.starknet.business_logic.fact_state.patricia_state import PatriciaStateReader
from starkware.starknet.business_logic.fact_state.state import SharedState
from starkware.starknet.business_logic.state.state import CachedState
from starkware.starknet.business_logic.state.state_api import State
from starkware.starknet.business_logic.state.state_api_objects import BlockInfo
from starkware.starknet.business_logic.transaction.objects import (
    InternalDeclare,
    InternalDeploy,
    InternalInvokeFunction,
    InternalTransaction,
)
from starkware.starknet.definitions import constants, fields
from starkware.starknet.definitions.general_config import StarknetGeneralConfig
from starkware.starknet.public.abi import get_selector_from_name
from starkware.starknet.services.api.contract_class.contract_class import (
    DeprecatedCompiledClass,
    EntryPointType,
)
from starkware.starknet.services.api.gateway.transaction import DEFAULT_DECLARE_SENDER_ADDRESS
from starkware.starknet.services.api.messages import StarknetMessageToL1
from starkware.storage.dict_storage import DictStorage
from starkware.storage.storage import FactFetchingContext

CastableToAddress = Union[str, int]
CastableToAddressSalt = Union[str, int]


class StarknetState:
    """
    StarkNet testing object. Represents a state of a StarkNet network.

    Example usage:
      starknet = await StarknetState.empty()
      contract_class = compile_starknet_files([CONTRACT_FILE], debug_info=True)
      contract_address, _ = await starknet.deploy(contract_class=contract_class)
      res = await starknet.invoke_raw(
          contract_address=contract_address, selector="func", calldata=[1, 2])
    """

    def __init__(self, state: CachedState, general_config: StarknetGeneralConfig):
        """
        Constructor. Should not be used directly. Use empty() instead.
        """
        self.state = state
        self.general_config = general_config
        # A mapping from L2-to-L1 message hash to its counter.
        self._l2_to_l1_messages: MutableMapping[str, int] = {}
        # A list of all L2-to-L1 messages sent, in chronological order.
        self.l2_to_l1_messages_log: List[StarknetMessageToL1] = []
        # A list of all events emitted, in chronological order.
        self.events: List[Event] = []

    def copy(self) -> "StarknetState":
        """
        Creates a new StarknetState instance with the same state. And modifications to one instance
        would not affect the other.
        """
        return copy.deepcopy(self)

    @classmethod
    async def empty(cls, general_config: Optional[StarknetGeneralConfig] = None) -> "StarknetState":
        """
        Creates a new StarknetState instance.
        """
        if general_config is None:
            general_config = StarknetGeneralConfig()

        ffc = FactFetchingContext(storage=DictStorage(), hash_func=pedersen_hash_func)
        empty_shared_state = await SharedState.empty(ffc=ffc, general_config=general_config)
        state_reader = PatriciaStateReader(
            global_state_root=empty_shared_state.contract_states,
            contract_class_root=empty_shared_state.contract_classes,
            ffc=ffc,
            contract_class_storage=ffc.storage,
        )
        state = CachedState(
            block_info=BlockInfo.empty(sequencer_address=general_config.sequencer_address),
            state_reader=state_reader,
            contract_class_cache={},
        )

        return cls(state=state, general_config=general_config)

    async def declare(
        self, contract_class: DeprecatedCompiledClass
    ) -> Tuple[int, TransactionExecutionInfo]:
        """
        Declares a contract class.
        Returns the class hash and the execution info.

        Args:
        contract_class - a compiled StarkNet contract returned by compile_starknet_files().
        """
        tx = InternalDeclare.create_deprecated(
            contract_class=contract_class,
            chain_id=self.general_config.chain_id.value,
            sender_address=DEFAULT_DECLARE_SENDER_ADDRESS,
            max_fee=0,
            version=0,
            signature=[],
            nonce=0,
        )
        self.state.contract_classes[tx.class_hash] = contract_class

        with self.state.copy_and_apply() as state_copy:
            tx_execution_info = await tx.apply_state_updates(
                state=state_copy, general_config=self.general_config
            )

        return tx.class_hash, tx_execution_info

    async def deploy(
        self,
        contract_class: DeprecatedCompiledClass,
        constructor_calldata: List[int],
        contract_address_salt: Optional[CastableToAddressSalt] = None,
    ) -> Tuple[int, TransactionExecutionInfo]:
        """
        Deploys a contract. Returns the contract address and the execution info.

        Args:
        contract_class - a compiled StarkNet contract returned by compile_starknet_files().
        contract_address_salt - If supplied, a hexadecimal string or an integer representing
        the salt to use for deploying. Otherwise, the salt is randomized.
        """
        if contract_address_salt is None:
            contract_address_salt = fields.ContractAddressSalt.get_random_value()
        if isinstance(contract_address_salt, str):
            contract_address_salt = int(contract_address_salt, 16)
        assert isinstance(contract_address_salt, int)

        tx = InternalDeploy.create(
            contract_address_salt=contract_address_salt,
            constructor_calldata=constructor_calldata,
            contract_class=contract_class,
            chain_id=self.general_config.chain_id.value,
            version=constants.TRANSACTION_VERSION,
        )

        self.state.contract_classes[from_bytes(tx.contract_hash)] = contract_class
        tx_execution_info = await self.execute_tx(tx=tx)

        return tx.contract_address, tx_execution_info

    async def invoke_raw(
        self,
        contract_address: CastableToAddress,
        selector: Union[int, str],
        calldata: List[int],
        max_fee: int,
        signature: Optional[List[int]] = None,
        nonce: Optional[int] = None,
    ) -> TransactionExecutionInfo:
        """
        Invokes a contract function. Returns the execution info.

        Args:
        contract_address - a hexadecimal string or an integer representing the contract address.
        selector - either a function name or an integer selector for the entrypoint to invoke.
        calldata - a list of integers to pass as calldata to the invoked function.
        signature - a list of integers to pass as signature to the invoked function.
        """
        tx = await create_invoke_function(
            state=self.state,
            contract_address=contract_address,
            selector=selector,
            calldata=calldata,
            max_fee=max_fee,
            version=constants.TRANSACTION_VERSION,
            signature=signature,
            nonce=nonce,
            chain_id=self.general_config.chain_id.value,
        )
        return await self.execute_tx(tx=tx)

    async def execute_entry_point_raw(
        self,
        contract_address: CastableToAddress,
        selector: Union[int, str],
        calldata: List[int],
        caller_address: int,
    ) -> CallInfo:
        """
        Builds the transaction execution context and executes the entry point.
        Returns the CallInfo.
        """
        if isinstance(contract_address, str):
            contract_address = int(contract_address, 16)
        assert isinstance(contract_address, int)

        if isinstance(selector, str):
            selector = get_selector_from_name(selector)
        assert isinstance(selector, int)

        call = ExecuteEntryPoint.create(
            contract_address=contract_address,
            entry_point_selector=selector,
            initial_gas=constants.GasCost.INITIAL.value,
            entry_point_type=EntryPointType.EXTERNAL,
            calldata=calldata,
            caller_address=caller_address,
        )

        with self.state.copy_and_apply() as state_copy:
            call_info = await call.execute_for_testing(
                state=state_copy,
                general_config=self.general_config,
            )

        self.add_messages_and_events(execution_info=call_info)

        return call_info

    async def execute_tx(self, tx: InternalTransaction) -> TransactionExecutionInfo:
        with self.state.copy_and_apply() as state_copy:
            tx_execution_info = await tx.apply_state_updates(
                state=state_copy, general_config=self.general_config
            )

        self.add_messages_and_events(execution_info=tx_execution_info)

        return tx_execution_info

    def add_messages_and_events(self, execution_info: Union[CallInfo, TransactionExecutionInfo]):
        # Add messages.
        for message in execution_info.get_sorted_l2_to_l1_messages():
            starknet_message = StarknetMessageToL1(
                from_address=message.from_address,
                to_address=message.to_address,
                payload=message.payload,
            )
            self.l2_to_l1_messages_log.append(starknet_message)
            message_hash = starknet_message.get_hash()
            self._l2_to_l1_messages[message_hash] = self._l2_to_l1_messages.get(message_hash, 0) + 1

        # Add events.
        self.events += execution_info.get_sorted_events()

    def consume_message_hash(self, message_hash: str):
        """
        Consumes the given message hash.
        """
        assert (
            self._l2_to_l1_messages.get(message_hash, 0) > 0
        ), f"Message of hash {message_hash} is fully consumed."

        self._l2_to_l1_messages[message_hash] -= 1


async def create_invoke_function(
    state: State,
    contract_address: CastableToAddress,
    selector: Union[int, str],
    calldata: List[int],
    max_fee: int,
    version: int,
    signature: Optional[List[int]],
    nonce: Optional[int],
    chain_id: int,
    only_query: bool = False,
) -> InternalInvokeFunction:
    if isinstance(contract_address, str):
        contract_address = int(contract_address, 16)
    assert isinstance(contract_address, int)

    if isinstance(selector, str):
        selector = get_selector_from_name(selector)
    assert isinstance(selector, int)

    if signature is None:
        signature = []

    # We allow not specifying nonce. In this case, the current nonce of the contract will be used.
    if nonce is None:
        nonce = await state.get_nonce_at(contract_address=contract_address)

    return InternalInvokeFunction.create(
        sender_address=contract_address,
        entry_point_selector=selector,
        calldata=calldata,
        max_fee=max_fee,
        signature=signature,
        nonce=nonce,
        chain_id=chain_id,
        version=version,
    )