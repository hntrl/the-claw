from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agents.realtime.model_inputs import (
    RealtimeModelSendEvent,
    RealtimeModelSendRawMessage,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
)
from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel


@dataclass
class ToolResponse:
    turn_id: int
    call_ids: set[str]
    completed: bool


class TurnAwareRealtimeModel(OpenAIRealtimeWebSocketModel):
    """Tag responses at submission and suppress interrupted tool continuations.

    The SDK's default send_message/tool-output helpers create untagged responses.
    Arrival time cannot identify their owner when new text races a server ack.
    Use the SDK's raw-message API for response metadata; its response sequencer
    still serializes response.create requests on the same WebSocket.
    """

    def __init__(
        self,
        current_turn: Callable[[], int | None],
        take_tool_turn: Callable[[str], int | None],
        is_current: Callable[[int], bool],
    ) -> None:
        super().__init__()
        self._current_turn = current_turn
        self._take_tool_turn = take_tool_turn
        self._is_current = is_current
        self._tool_responses: dict[str, str] = {}
        self._finished_responses: dict[str, ToolResponse] = {}
        self._sent_tool_outputs: set[str] = set()
        self.add_listener(self)

    async def on_event(self, event: Any) -> None:
        if event.type != "raw_server_event":
            return
        data = event.data
        if data.get("type") == "response.output_item.done":
            item = data.get("item", {})
            if item.get("type") == "function_call":
                self._tool_responses[item["call_id"]] = data["response_id"]
        elif data.get("type") == "response.done":
            response = data["response"]
            call_ids = {
                item["call_id"]
                for item in response.get("output", [])
                if item.get("type") == "function_call"
            }
            turn_id = (response.get("metadata") or {}).get("claw_turn_id")
            if call_ids and isinstance(turn_id, str) and turn_id.isdecimal():
                self._finished_responses[response["id"]] = ToolResponse(
                    int(turn_id), call_ids, response.get("status") == "completed"
                )
                await self._continue_after_tools(response["id"])

    async def _continue_after_tools(self, response_id: str) -> None:
        response = self._finished_responses.get(response_id)
        if response is None or not response.call_ids <= self._sent_tool_outputs:
            return
        del self._finished_responses[response_id]
        self._sent_tool_outputs.difference_update(response.call_ids)
        for call_id in response.call_ids:
            self._tool_responses.pop(call_id, None)
        # One response can contain several tools. Wait for the server's complete
        # inventory and all results so they produce exactly one continuation.
        if response.completed and self._is_current(response.turn_id):
            await self._create_response(response.turn_id)

    async def send_event(self, event: RealtimeModelSendEvent) -> None:
        if isinstance(event, RealtimeModelSendUserInput):
            turn_id = self._current_turn()
            if turn_id is None:
                return
            message = event.user_input
            if isinstance(message, str):
                message = {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                }
            await super().send_event(
                RealtimeModelSendRawMessage(
                    message={
                        "type": "conversation.item.create",
                        "other_data": {"item": message},
                    }
                )
            )
            await self._create_response(turn_id)
        elif isinstance(event, RealtimeModelSendToolOutput):
            self._take_tool_turn(event.tool_call.call_id)
            # Keep the tool result in history, including interrupted results.
            await super().send_event(replace(event, start_response=False))
            self._sent_tool_outputs.add(event.tool_call.call_id)
            response_id = self._tool_responses.get(event.tool_call.call_id)
            if response_id is not None:
                await self._continue_after_tools(response_id)
        else:
            await super().send_event(event)

    async def close(self) -> None:
        await super().close()
        self._tool_responses.clear()
        self._finished_responses.clear()
        self._sent_tool_outputs.clear()

    async def _create_response(self, turn_id: int) -> None:
        await super().send_event(
            RealtimeModelSendRawMessage(
                message={
                    "type": "response.create",
                    "other_data": {
                        "response": {"metadata": {"claw_turn_id": str(turn_id)}}
                    },
                }
            )
        )
