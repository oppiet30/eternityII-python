import hashlib
import json
import random

import pytest

from checkpoint_solver import solve_resumable
from job_tools import DATA, OFFICIAL_STARTER, batch_sql, partition, search_policy
from piece_loader import load_pieces
from search_model import SearchModel, partial_score
from solver import Piece, solve


PIECES = [Piece(1, (0, 1, 2, 0)), Piece(2, (0, 0, 3, 1)),
          Piece(3, (2, 4, 0, 0)), Piece(4, (3, 0, 0, 4))]


def test_disjoint_partitions_cover_all_small_puzzle_solutions():
    expected = set(solve(PIECES))
    model = SearchModel(PIECES)
    leaves = partition(PIECES, depth=2)
    found = []
    for leaf in leaves:
        prefix = tuple(model.decode(item) for item in leaf)
        found.extend(board for board in expected if board[:2] == prefix)
    assert set(found) == expected
    assert len(found) == len(set(found))
    assert len(leaves) == len({json.dumps(leaf) for leaf in leaves})
    with pytest.raises(ValueError, match='max_jobs'):
        partition(PIECES, depth=1, max_jobs=1)


def test_fixed_piece_is_reserved_and_enforced():
    fixed = [dict(row=1, col=1, piece_id=4, rotation=0)]
    model = SearchModel(PIECES, fixed=fixed)
    assert all(p.id != 4 for p in model.candidates())
    board = solve_resumable(PIECES, fixed=fixed)['board']
    assert board[3] == PIECES[3]
    with pytest.raises(ValueError, match='prefix'):
        solve_resumable(PIECES, prefix=[PIECES[3].rotated(2)], fixed=fixed)


def test_forward_pruning_preserves_answers_and_reduces_nodes():
    unsatisfiable = PIECES[:3] + [Piece(4, (8, 0, 0, 4))]
    slow = solve_resumable(unsatisfiable, forward_check=False)
    states = []
    fast = solve_resumable(unsatisfiable, save_checkpoint=states.append)
    assert fast['board'] == slow['board'] is None
    assert fast['nodes'] < slow['nodes']
    assert states[-1]['pruned'] > 0
    assert states[-1]['outcome'] == 'exhausted'


def test_partial_statistics_are_real_placements():
    states = []
    result = solve_resumable(PIECES, save_checkpoint=states.append, checkpoint_interval=0)
    for state in states:
        depth = len(state['best_board'])
        assert state['best_score'] == partial_score(depth, 2)
        assert state['elapsed_seconds'] >= 0
        assert state['nodes_per_second'] >= 0
    assert result['best_score'] == 4
    assert states[-1]['outcome'] == 'solved'


def test_legacy_checkpoint_resumes_original_candidate_order():
    identity = [2, [(p.id, p.edges) for p in PIECES], []]
    checkpoint = dict(version=1, fingerprint=hashlib.sha256(json.dumps(
        identity, separators=(',', ':')).encode()).hexdigest(),
        next_indices=[1, 1, 0], nodes=2, max_depth=2)
    states = []
    result = solve_resumable(PIECES, checkpoint=checkpoint, save_checkpoint=states.append)
    assert result['board'] == next(solve(PIECES))
    assert result['nodes'] == 4
    assert states[-1]['version'] == 1
    with pytest.raises(ValueError, match='legacy'):
        solve_resumable(PIECES, checkpoint=checkpoint,
                        fixed=[dict(row=1, col=1, piece_id=4, rotation=0)])


def test_policy_change_rejects_v2_checkpoint():
    states = []
    solve_resumable(PIECES, save_checkpoint=states.append)
    with pytest.raises(ValueError, match='does not match'):
        solve_resumable(PIECES, checkpoint=states[-1], forward_check=False)


def test_bundled_official_starter_and_partition():
    pieces = load_pieces(DATA)
    fixed, forward, official = search_policy(pieces, {})
    assert official and forward and fixed == [OFFICIAL_STARTER]
    assert pieces[138].rotated(2).edges == (18, 6, 6, 11)
    assert len(partition(pieces, depth=1, fixed=fixed)) == 4
    assert search_policy(pieces, {}, legacy=True) == ([], False, False)


def test_batch_plan_is_safe_and_does_not_submit():
    plan = batch_sql(partition(PIECES), dict(note="quote'\\value"))
    assert 'proc_enqueue_partition_batch' in plan
    assert "quote'" not in plan
    assert 'CONVERT(X' in plan
    with pytest.raises(ValueError, match='no viable'):
        batch_sql([], {})


@pytest.mark.parametrize('seed', range(6))
def test_pruned_search_matches_exhaustive_oracle(seed):
    rng = random.Random(seed)
    size = 3
    horizontal = [[rng.randint(1, 3) for _ in range(size - 1)] for _ in range(size)]
    vertical = [[rng.randint(1, 3) for _ in range(size)] for _ in range(size - 1)]
    pieces = []
    for row in range(size):
        for col in range(size):
            edges = (vertical[row - 1][col] if row else 0,
                     horizontal[row][col] if col < size - 1 else 0,
                     vertical[row][col] if row < size - 1 else 0,
                     horizontal[row][col - 1] if col else 0)
            pieces.append(Piece(len(pieces) + 1, edges).rotated(rng.randrange(4)))
    rng.shuffle(pieces)
    assert solve_resumable(pieces)['board'] == next(solve(pieces), None)
    # A unique non-border motif on one tile can never be matched.
    broken = list(pieces)
    item = broken[0]
    edges = list(item.edges)
    edges[next(i for i, value in enumerate(edges) if value)] = 99
    broken[0] = Piece(item.id, tuple(edges))
    assert solve_resumable(broken)['board'] == next(solve(broken), None) is None
