from __future__ import annotations

import pytest

from .dataset_utils import load_all_datasets, replay_dataset_via_parser


@pytest.mark.parametrize("dataset", load_all_datasets(), ids=lambda item: item.name)
def test_dataset_protocol_replay_matches_expected(dataset) -> None:
    replay = replay_dataset_via_parser(dataset)

    assert replay.event_sequence == dataset.expected.event_sequence
    assert replay.instruction_names == dataset.expected.instruction_names
    assert replay.summaries == dataset.expected.summaries
    assert replay.degraded_count == dataset.expected.degraded_count
    assert replay.terminal_playing_state == dataset.expected.terminal_playing_state
