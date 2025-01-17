# Copyright 2020 Adap GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contextmanager for a gRPC request-response channel to the Flower server."""


from contextlib import contextmanager
from logging import DEBUG, ERROR, WARN
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple, Union, cast

from flwr.client.message_handler.task_handler import get_server_message
from flwr.common import GRPC_MAX_MESSAGE_LENGTH
from flwr.common.grpc import create_channel
from flwr.common.logger import log
from flwr.proto.fleet_pb2 import (
    CreateNodeRequest,
    DeleteNodeRequest,
    PullTaskInsRequest,
    PushTaskResRequest,
)
from flwr.proto.fleet_pb2_grpc import FleetStub
from flwr.proto.node_pb2 import Node
from flwr.proto.task_pb2 import Task, TaskIns, TaskRes
from flwr.proto.transport_pb2 import ClientMessage, ServerMessage

KEY_NODE = "node"
KEY_TASK_INS = "current_task_ins"


def on_channel_state_change(channel_connectivity: str) -> None:
    """Log channel connectivity."""
    log(DEBUG, channel_connectivity)


@contextmanager
def grpc_request_response(
    server_address: str,
    max_message_length: int = GRPC_MAX_MESSAGE_LENGTH,  # pylint: disable=W0613
    root_certificates: Optional[
        Union[bytes, str]
    ] = None,  # pylint: disable=unused-argument
) -> Iterator[
    Tuple[
        Callable[[], Optional[ServerMessage]],
        Callable[[ClientMessage], None],
        Optional[Callable[[], None]],
        Optional[Callable[[], None]],
    ]
]:
    """Primitives for request/response-based interaction with a server.

    One notable difference to the grpc_connection context manager is that
    `receive` can return `None`.

    Parameters
    ----------
    server_address : str
        The IPv6 address of the server with `http://` or `https://`.
        If the Flower server runs on the same machine
        on port 8080, then `server_address` would be `"http://[::]:8080"`.
    max_message_length : int
        Ignored, only present to preserve API-compatibility.
    root_certificates : Optional[Union[bytes, str]] (default: None)
        Path of the root certificate. If provided, a secure
        connection using the certificates will be established to an SSL-enabled
        Flower server. Bytes won't work for the REST API.

    Returns
    -------
    receive : Callable
    send : Callable
    create_node : Optional[Callable]
    delete_node : Optional[Callable]
    """
    if isinstance(root_certificates, str):
        root_certificates = Path(root_certificates).read_bytes()

    channel = create_channel(
        server_address=server_address,
        root_certificates=root_certificates,
        max_message_length=max_message_length,
    )
    channel.subscribe(on_channel_state_change)
    stub = FleetStub(channel)

    log(
        WARN,
        """
        EXPERIMENTAL: `grpc-rere` is an experimental transport layer, it might change
        considerably in future versions of Flower
        """,
    )

    # Necessary state to link TaskRes to TaskIns
    state: Dict[str, Optional[TaskIns]] = {KEY_TASK_INS: None}

    # Enable create_node and delete_node to store node
    node_store: Dict[str, Optional[Node]] = {KEY_NODE: None}

    ###########################################################################
    # receive/send functions
    ###########################################################################

    def create_node() -> None:
        """Set create_node."""
        create_node_request = CreateNodeRequest()
        create_node_response = stub.CreateNode(
            request=create_node_request,
        )
        node_store[KEY_NODE] = create_node_response.node

    def delete_node() -> None:
        """Set delete_node."""
        # Get Node
        if node_store[KEY_NODE] is None:
            log(ERROR, "Node instance missing")
            return
        node: Node = cast(Node, node_store[KEY_NODE])

        delete_node_request = DeleteNodeRequest(node=node)
        stub.DeleteNode(request=delete_node_request)

    def receive() -> Optional[ServerMessage]:
        """Receive next task from server."""
        # Get Node
        if node_store[KEY_NODE] is None:
            log(ERROR, "Node instance missing")
            return None
        node: Node = cast(Node, node_store[KEY_NODE])

        # Request instructions (task) from server
        request = PullTaskInsRequest(node=node)
        response = stub.PullTaskIns(request=request)

        # Remember the current TaskIns
        task_ins_server_message_tuple = get_server_message(response)
        if task_ins_server_message_tuple is None:
            state[KEY_TASK_INS] = None
            return None

        task_ins, server_message = task_ins_server_message_tuple

        # Remember `task_ins` until `task_res` is available
        state[KEY_TASK_INS] = task_ins

        # Return the ServerMessage
        return server_message

    def send(client_message_proto: ClientMessage) -> None:
        """Send task result back to server."""
        # Get Node
        if node_store[KEY_NODE] is None:
            log(ERROR, "Node instance missing")
            return
        node: Node = cast(Node, node_store[KEY_NODE])

        # Get incoming TaskIns
        if state[KEY_TASK_INS] is None:
            log(ERROR, "No current TaskIns")
            return
        task_ins: TaskIns = cast(TaskIns, state[KEY_TASK_INS])

        # Wrap ClientMessage in TaskRes
        task_res = TaskRes(
            task_id="",  # This will be generated by the server
            group_id=task_ins.group_id,
            workload_id=task_ins.workload_id,
            task=Task(
                producer=node,
                consumer=task_ins.task.producer,
                legacy_client_message=client_message_proto,
                ancestry=[task_ins.task_id],
            ),
        )

        # Serialize ProtoBuf to bytes
        request = PushTaskResRequest(task_res_list=[task_res])
        _ = stub.PushTaskRes(request)

        state[KEY_TASK_INS] = None

    try:
        # Yield methods
        yield (receive, send, create_node, delete_node)
    except Exception as exc:  # pylint: disable=broad-except
        log(ERROR, exc)
