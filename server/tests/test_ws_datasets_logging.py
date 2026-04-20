from __future__ import annotations

from .dataset_utils import load_all_datasets, replay_dataset_via_parser


def test_weather_query_dataset_contains_expected_summaries() -> None:
    dataset = next(item for item in load_all_datasets() if item.name == "weather_query")
    replay = replay_dataset_via_parser(dataset)

    assert "Query" in replay.summaries
    assert any(summary.startswith("text=重庆渝北今天18℃到28℃") for summary in replay.summaries)
    assert replay.terminal_playing_state == "Idle"


def test_tv_control_dataset_contains_instruction_control() -> None:
    dataset = next(item for item in load_all_datasets() if item.name == "tv_control")
    replay = replay_dataset_via_parser(dataset)

    assert "InstructionControl" in replay.instruction_names
    assert "behavior=INSERT_FRONT" in replay.summaries
    assert "state=Playing" in replay.summaries
    assert replay.terminal_playing_state == "Idle"


def test_wake_only_dataset_supports_missing_optional_fields() -> None:
    dataset = next(item for item in load_all_datasets() if item.name == "wake_only")
    replay = replay_dataset_via_parser(dataset)

    assert replay.degraded_count == 0
    assert "stop_time=None" in replay.summaries
    assert "text=- is_final=True results=1" in replay.summaries
    assert replay.terminal_playing_state is None
