from typing import Any, Optional, Union, cast

from pydantic import Field, RootModel, field_validator

from evm_trace.base import BaseModel, CallTreeNode
from evm_trace.enums import CallType


class CallAction(BaseModel):
    gas: int
    """
    The amount of gas available for the action.
    """

    input: Optional[str] = None
    receiver: Optional[str] = Field(alias="to", default=None)
    sender: str = Field(alias="from")
    value: int
    # only used to recover the specific call type
    call_type: str = Field(alias="callType", repr=False)

    @field_validator("value", "gas", mode="before")
    def convert_integer(cls, v):
        return int(v, 16)


class CreateAction(BaseModel):
    gas: int
    """
    The amount of gas available for the action.
    """

    init: str
    value: int

    @field_validator("value", "gas", mode="before")
    def convert_integer(cls, v):
        return int(v, 16)


class SelfDestructAction(BaseModel):
    address: str
    balance: int

    @field_validator("balance", mode="before")
    def convert_integer(cls, v):
        return int(v, 16) if isinstance(v, str) else int(v)


class ActionResult(BaseModel):
    """
    A base class for various OP-code-specified actions
    in Parity ``trace_transaction`` output.
    """

    gas_used: int = Field(alias="gasUsed")
    """
    The amount of gas utilized by the action. It does *not*
    include the ``21,000`` base fee or the data costs of ``4``
    for gas per zero byte and ``16`` gas per non-zero byte.
    """

    @field_validator("gas_used", mode="before")
    def convert_integer(cls, v):
        return int(v, 16) if isinstance(v, str) else int(v)


class CallResult(ActionResult):
    """
    The result of CALL.
    """

    output: str


class CreateResult(ActionResult):
    """
    The result of CREATE.
    """

    address: str
    code: str


ParityTraceAction = Union[CreateAction, CallAction, SelfDestructAction]
ParityTraceResult = Union[CallResult, CreateResult]


class ParityTrace(BaseModel):
    error: Optional[str] = None
    action: ParityTraceAction
    block_hash: str = Field(alias="blockHash")
    call_type: CallType = Field(alias="type")
    result: Optional[ParityTraceResult] = None
    subtraces: int
    trace_address: list[int] = Field(alias="traceAddress")
    transaction_hash: str = Field(alias="transactionHash")

    @field_validator("call_type", mode="before")
    def convert_call_type(cls, value, info) -> CallType:
        if isinstance(info.data["action"], CallAction):
            value = info.data["action"].call_type

        value = value.upper()
        if value == "SUICIDE":
            value = "SELFDESTRUCT"

        return CallType(value)


ParityTraceList = RootModel[list[ParityTrace]]


def get_calltree_from_parity_trace(
    traces: ParityTraceList,
    root: Optional[ParityTrace] = None,
    **root_kwargs,
) -> CallTreeNode:
    """
    Create a :class:`~evm_trace.base.CallTreeNode` from output models using the Parity approach
    (e.g. from the ``trace_transaction`` RPC).

    Args:
        traces (:class:~evm_trace.parity.ParityTraceList): The list of parity trace nodes,
          likely loaded from the response data from the ``trace_transaction`` RPC response.
        root (:class:`~evm_trace.parity.ParityTrace`): The root parity trace node. Optional, uses
          the first item by default.
        **root_kwargs: Additional kwargs to append to the root node. Useful for adding gas for
          reverted calls.

    Returns:
        :class:`~evm_trace.base.CallTreeNode`
    """
    root = root or traces.root[0]
    failed = root.error is not None
    node_kwargs: dict[Any, Any] = {
        "call_type": root.call_type,
        "failed": failed,
    }

    if root.call_type == CallType.CREATE:
        create_action: CreateAction = cast(CreateAction, root.action)
        create_result: Optional[CreateResult] = (
            cast(CreateResult, root.result) if root.result is not None else None
        )
        node_kwargs.update(
            value=create_action.value,
            gas_limit=create_action.gas,
            calldata=create_action.init,
        )
        if create_result:
            node_kwargs.update(gas_cost=create_result.gas_used, address=create_result.address)

    elif root.call_type in (
        CallType.CALL,
        CallType.DELEGATECALL,
        CallType.STATICCALL,
        CallType.CALLCODE,
    ):
        call_action: CallAction = cast(CallAction, root.action)
        call_result: Optional[CallResult] = (
            cast(CallResult, root.result) if root.result is not None else None
        )

        node_kwargs.update(
            address=call_action.receiver,
            value=call_action.value,
            gas_limit=call_action.gas,
            calldata=call_action.input,
        )
        # no result if the call has an error
        if call_result:
            node_kwargs.update(
                gas_cost=call_result.gas_used,
                returndata=call_result.output,
            )

    elif root.call_type == CallType.SELFDESTRUCT:
        selfdestruct_action: SelfDestructAction = cast(SelfDestructAction, root.action)
        node_kwargs.update(
            address=selfdestruct_action.address,
        )

    trace_list: list[ParityTrace] = traces.root
    subtraces: list[ParityTrace] = [
        sub
        for sub in trace_list
        if len(sub.trace_address) == len(root.trace_address) + 1
        and sub.trace_address[:-1] == root.trace_address
    ]
    node_kwargs["calls"] = [get_calltree_from_parity_trace(traces, root=sub) for sub in subtraces]
    node_kwargs = {**node_kwargs, **root_kwargs}
    return CallTreeNode.model_validate(node_kwargs)
