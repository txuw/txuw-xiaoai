from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from txuw_xiaoai_server.protocol import (
    InboundMessage,
    InstructionEventMessage,
    KwsEventMessage,
    PlayingEventMessage,
    UnknownEventMessage,
    parse_text_message,
)
from txuw_xiaoai_server.socket_logging import build_socket_log_entry


DATASET_DIR = Path(__file__).parent / "datasets"


class DatasetExpectation(BaseModel):
    event_sequence: list[str] = Field(default_factory=list)
    instruction_names: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(default_factory=list)
    degraded_count: int = 0
    terminal_playing_state: str | None = None


class ConversationDataset(BaseModel):
    name: str
    description: str
    frames: list[str] = Field(default_factory=list)
    expected: DatasetExpectation


class ReplayResult(BaseModel):
    messages: list[InboundMessage] = Field(default_factory=list)
    event_sequence: list[str] = Field(default_factory=list)
    instruction_names: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(default_factory=list)
    degraded_count: int = 0
    terminal_playing_state: str | None = None


def load_dataset(name: str) -> ConversationDataset:
    path = DATASET_DIR / f"{name}.json"
    return ConversationDataset.model_validate_json(path.read_text(encoding="utf-8"))


def load_all_datasets() -> list[ConversationDataset]:
    return [
        ConversationDataset.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(DATASET_DIR.glob("*.json"))
    ]


def replay_dataset_via_parser(dataset: ConversationDataset) -> ReplayResult:
    messages: list[InboundMessage] = []
    event_sequence: list[str] = []
    instruction_names: list[str] = []
    summaries: list[str] = []
    degraded_count = 0
    terminal_playing_state: str | None = None

    for raw_payload in dataset.frames:
        message = parse_text_message(raw_payload)
        messages.append(message)
        event_sequence.append(_extract_event_name(message))

        entry = build_socket_log_entry(message, "dataset-replay", frame_type="text")
        summaries.append(entry.summary)
        if entry.status == "degraded":
            degraded_count += 1

        instruction_name = _extract_instruction_name(message)
        if instruction_name is not None:
            instruction_names.append(instruction_name)

        if isinstance(message, PlayingEventMessage) and message.data is not None:
            terminal_playing_state = message.data.state.value

    return ReplayResult(
        messages=messages,
        event_sequence=event_sequence,
        instruction_names=instruction_names,
        summaries=summaries,
        degraded_count=degraded_count,
        terminal_playing_state=terminal_playing_state,
    )


def _extract_event_name(message: InboundMessage) -> str:
    if isinstance(message, InstructionEventMessage):
        return message.event
    if isinstance(message, PlayingEventMessage):
        return message.event
    if isinstance(message, KwsEventMessage):
        return message.event
    if isinstance(message, UnknownEventMessage):
        return message.event
    return message.message_type


def _extract_instruction_name(message: InboundMessage) -> str | None:
    if not isinstance(message, InstructionEventMessage):
        return None
    if getattr(message.data, "decoded_envelope", None) is None:
        return None
    return message.data.decoded_envelope.header.name
