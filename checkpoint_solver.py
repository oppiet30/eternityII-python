"""Resumable exact search with genuine statistics and frontier pruning."""
import hashlib
import json
import math
import time

from search_model import SearchModel, partial_score


def solve_resumable(pieces, size=None, prefix=(), checkpoint=None,
                    save_checkpoint=lambda state: None, check_alive=lambda: None,
                    checkpoint_interval=30, fixed=(), forward_check=True):
    legacy = checkpoint is not None and checkpoint.get('version') == 1
    if legacy:
        if fixed:
            raise ValueError('legacy checkpoints cannot add fixed placements')
        forward_check = False
    model = SearchModel(pieces, size, fixed)
    n = model.n
    if len(prefix) > n * n:
        raise ValueError('prefix exceeds board size')
    for piece in prefix:
        if piece not in model.candidates():
            raise ValueError('prefix is not a valid row-major placement')
        model.push(piece)
    base = len(model.board)
    identity = [n, [(p.id, p.edges) for p in pieces], [(p.id, p.edges) for p in prefix]]
    if not legacy:
        identity += [[(pos, p.id, p.edges) for pos, p in sorted(model.fixed.items())], forward_check]
    fingerprint = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    cursors, nodes, max_depth, pruned, elapsed = [0], 0, base, 0, 0.0
    best = list(model.board)
    if checkpoint is not None:
        if checkpoint.get('version') not in (1, 2) or checkpoint.get('fingerprint') != fingerprint:
            raise ValueError('checkpoint does not match this search')
        cursors = checkpoint['next_indices'][:]
        if (not cursors or len(cursors) > n * n - base + 1
                or any(type(i) is not int or i < 0 for i in cursors)):
            raise ValueError('invalid checkpoint cursors')
        nodes, max_depth = checkpoint['nodes'], checkpoint['max_depth']
        if type(nodes) is not int or nodes < 0 or type(max_depth) is not int or not base <= max_depth <= n * n:
            raise ValueError('invalid checkpoint statistics')
        pruned, elapsed = checkpoint.get('pruned', 0), checkpoint.get('elapsed_seconds', 0.0)
        if (type(pruned) is not int or pruned < 0 or not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed) or elapsed < 0):
            raise ValueError('invalid checkpoint statistics')
        for cursor in cursors[:-1]:
            options = model.candidates()
            if not 1 <= cursor <= len(options):
                raise ValueError('invalid checkpoint path')
            model.push(options[cursor - 1])
        if len(model.board) < n * n and cursors[-1] > len(model.candidates()):
            raise ValueError('invalid checkpoint cursor')
        if len(model.board) > max_depth or nodes < len(model.board) - base:
            raise ValueError('invalid checkpoint statistics')
        if checkpoint.get('best_board') is not None:
            verifier = SearchModel(pieces, n, fixed)
            best = []
            for item in checkpoint['best_board']:
                if len(best) >= n * n:
                    raise ValueError('invalid checkpoint best board')
                piece = verifier.decode(item)
                if piece not in verifier.candidates():
                    raise ValueError('invalid checkpoint best board')
                verifier.push(piece)
                best.append(piece)
            if len(best) < base or tuple(best[:base]) != tuple(prefix):
                raise ValueError('checkpoint best board belongs to a different prefix')
            if len(best) > max_depth:
                raise ValueError('invalid checkpoint best board depth')
        elif len(model.board) > len(best):
            best = list(model.board)
    started = time.monotonic()

    def snapshot():
        total_time = elapsed + time.monotonic() - started
        return dict(version=1 if legacy else 2, fingerprint=fingerprint,
                    next_indices=cursors[:], nodes=nodes, max_depth=max_depth,
                    current_depth=len(model.board), pruned=pruned,
                    elapsed_seconds=total_time, nodes_per_second=nodes / total_time if total_time else 0,
                    size=n, best_score=partial_score(len(best), n),
                    best_board=[model.encode(p) for p in best], outcome='searching')

    last_saved = started
    while True:
        check_alive()
        if time.monotonic() - last_saved >= checkpoint_interval:
            save_checkpoint(snapshot())
            last_saved = time.monotonic()
        if len(model.board) == n * n:
            state = snapshot()
            state['outcome'] = 'solved'
            save_checkpoint(state)
            return dict(board=tuple(model.board), nodes=nodes, depth=len(model.board),
                        best_score=state['best_score'])
        options = model.candidates()
        if cursors[-1] >= len(options):
            if len(model.board) == base:
                state = snapshot()
                state['outcome'] = 'exhausted'
                save_checkpoint(state)
                return dict(board=None, nodes=nodes, depth=max_depth,
                            best_score=state['best_score'])
            model.pop()
            cursors.pop()
            continue
        piece = options[cursors[-1]]
        cursors[-1] += 1
        nodes += 1
        model.push(piece)
        max_depth = max(max_depth, len(model.board))
        if len(model.board) > len(best):
            best = list(model.board)
        if forward_check and not model.forward_possible():
            pruned += 1
            model.pop()
        else:
            cursors.append(0)
