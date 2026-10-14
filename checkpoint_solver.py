"""Resumable depth-first search for a single exact solution."""
import hashlib
import json
import time
from math import isqrt

from solver import _candidate_index


def solve_resumable(pieces, size=None, prefix=(), checkpoint=None,
                    save_checkpoint=lambda state: None, check_alive=lambda: None,
                    checkpoint_interval=30):
    n = isqrt(len(pieces)) if size is None else size
    if n < 1 or n * n != len(pieces):
        raise ValueError("piece count must equal size squared")
    if len({p.id for p in pieces}) != len(pieces):
        raise ValueError("piece IDs must be unique")
    if any(len(p.edges) != 4 for p in pieces):
        raise ValueError("every piece needs four edges")
    index = _candidate_index(pieces)
    board, used = [], set()

    def candidates():
        row, col = divmod(len(board), n)
        mask = (row == 0, col == n - 1, row == n - 1, col == 0)
        top = board[-n].edges[2] if row else 0
        left = board[-1].edges[1] if col else 0
        return [p for p in index.get((mask, top, left), ()) if p.id not in used]

    if len(prefix) > n * n:
        raise ValueError("prefix exceeds board size")
    for piece in prefix:
        if piece not in candidates():
            raise ValueError("prefix is not a valid row-major placement")
        board.append(piece)
        used.add(piece.id)
    base = len(board)
    fingerprint = hashlib.sha256(json.dumps(
        [n, [(p.id, p.edges) for p in pieces], [(p.id, p.edges) for p in prefix]],
        separators=(',', ':')).encode()).hexdigest()
    cursors, nodes, max_depth = [0], 0, base
    if checkpoint is not None:
        if checkpoint.get('version') != 1 or checkpoint.get('fingerprint') != fingerprint:
            raise ValueError("checkpoint does not match this search")
        cursors = checkpoint['next_indices'][:]
        if (not cursors or len(cursors) > n * n - base + 1
                or any(type(i) is not int or i < 0 for i in cursors)):
            raise ValueError("invalid checkpoint cursors")
        nodes, max_depth = checkpoint['nodes'], checkpoint['max_depth']
        if type(nodes) is not int or nodes < 0 or type(max_depth) is not int or not base <= max_depth <= n * n:
            raise ValueError("invalid checkpoint statistics")
        for cursor in cursors[:-1]:
            options = candidates()
            if not 1 <= cursor <= len(options):
                raise ValueError("invalid checkpoint path")
            piece = options[cursor - 1]
            board.append(piece)
            used.add(piece.id)
        if len(board) < n * n and cursors[-1] > len(candidates()):
            raise ValueError("invalid checkpoint cursor")

    def snapshot():
        return dict(version=1, fingerprint=fingerprint, next_indices=cursors[:],
                    nodes=nodes, max_depth=max_depth)

    last_saved = time.monotonic()
    while True:
        check_alive()
        if time.monotonic() - last_saved >= checkpoint_interval:
            save_checkpoint(snapshot())
            last_saved = time.monotonic()
        if len(board) == n * n:
            save_checkpoint(snapshot())
            return dict(board=tuple(board), nodes=nodes, depth=len(board))
        options = candidates()
        if cursors[-1] >= len(options):
            if len(board) == base:
                save_checkpoint(snapshot())
                return dict(board=None, nodes=nodes, depth=max_depth)
            used.remove(board.pop().id)
            cursors.pop()
            continue
        piece = options[cursors[-1]]
        cursors[-1] += 1
        nodes += 1
        board.append(piece)
        used.add(piece.id)
        cursors.append(0)
        max_depth = max(max_depth, len(board))
