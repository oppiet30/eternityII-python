import copy

import pytest

from checkpoint_solver import solve_resumable
from solver import Piece, solve


PIECES = [Piece(1, (0, 1, 2, 0)), Piece(2, (0, 0, 3, 1)),
          Piece(3, (2, 4, 0, 0)), Piece(4, (3, 0, 0, 4))]


def test_checkpoint_resumes_exact_search():
    saved = []

    def interrupt(state):
        saved.append(copy.deepcopy(state))
        if state['nodes'] == 2:
            raise InterruptedError

    with pytest.raises(InterruptedError):
        solve_resumable(PIECES, save_checkpoint=interrupt, checkpoint_interval=0)
    result = solve_resumable(PIECES, checkpoint=saved[-1])
    assert result['board'] == next(solve(PIECES))
    assert result['nodes'] == solve_resumable(PIECES)['nodes']


def test_checkpoint_rejects_changed_input():
    states = []
    solve_resumable(PIECES, save_checkpoint=states.append)
    with pytest.raises(ValueError, match='does not match'):
        solve_resumable(list(reversed(PIECES)), checkpoint=states[-1])


def test_exhausted_and_finished_checkpoints_are_repeatable():
    for pieces in (PIECES, [Piece(i, (1, 1, 1, 1)) for i in range(4)]):
        states = []
        result = solve_resumable(pieces, save_checkpoint=states.append)
        assert solve_resumable(pieces, checkpoint=states[-1]) == result


def test_valid_prefix_and_bad_checkpoint():
    states = []
    result = solve_resumable(PIECES, prefix=[PIECES[0]], save_checkpoint=states.append)
    assert result['board'][0] == PIECES[0]
    states[-1]['next_indices'] = [100, 0]
    with pytest.raises(ValueError, match='invalid checkpoint'):
        solve_resumable(PIECES, prefix=[PIECES[0]], checkpoint=states[-1])
    with pytest.raises(ValueError, match='prefix'):
        solve_resumable(PIECES, prefix=[PIECES[0], PIECES[0]])


def test_all_backtracking_positions_resume_without_revisiting_search():
    unsatisfiable = PIECES[:3] + [Piece(4, (8, 0, 0, 4))]
    states = []
    result = solve_resumable(unsatisfiable, save_checkpoint=states.append,
                             checkpoint_interval=0)
    assert result['board'] is None
    assert result['depth'] > 1
    assert any(len(right['next_indices']) < len(left['next_indices'])
               for left, right in zip(states, states[1:]))
    for state in states:
        assert solve_resumable(unsatisfiable, checkpoint=state) == result
