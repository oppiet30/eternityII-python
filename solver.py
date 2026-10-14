"""Constraint-based edge-matching solver with preindexed rotations.

Edges are ordered (top, right, bottom, left); 0 means outside border.
"""
from collections import defaultdict
from dataclasses import dataclass
from math import isqrt


@dataclass(frozen=True)
class Piece:
    id: int
    edges: tuple[int, int, int, int]

    def rotated(self, turns: int) -> "Piece":
        """Rotate clockwise by 90 degrees per turn."""
        turns %= 4
        e = self.edges
        return Piece(self.id, e[-turns:] + e[:-turns] if turns else e)


def _candidate_index(pieces):
    """Index unique orientations by border mask and required top/left edges."""
    index = defaultdict(list)
    for piece in pieces:
        for edges in sorted({piece.rotated(k).edges for k in range(4)}):
            top, right, bottom, left = edges
            mask = tuple(edge == 0 for edge in edges)
            index[(mask, top, left)].append(Piece(piece.id, edges))
    return index


def solve(pieces: list[Piece], size: int | None = None):
    """Yield valid boards using each piece once.

    Precomputes rotations and indexes candidates by border position and
    already-placed neighbors. This reduces candidate scans but does not make
    exhaustive 16x16 search tractable on its own.
    """
    n = isqrt(len(pieces)) if size is None else size
    if n < 1 or n * n != len(pieces):
        raise ValueError("piece count must equal size squared")
    if len({p.id for p in pieces}) != len(pieces):
        raise ValueError("piece IDs must be unique")
    if any(len(p.edges) != 4 for p in pieces):
        raise ValueError("every piece needs four edges")
    index = _candidate_index(pieces)
    # Wildcards on the top/left edges are used only at the board boundary.
    boundary_index = defaultdict(list)
    for (mask, top, left), variants in index.items():
        boundary_index[(mask, top if not mask[0] else None,
                        left if not mask[3] else None)].extend(variants)

    board = []
    used = set()

    def search():
        pos = len(board)
        if pos == n * n:
            yield tuple(board)
            return
        row, col = divmod(pos, n)
        mask = (row == 0, col == n - 1, row == n - 1, col == 0)
        top = board[pos - n].edges[2] if row else None
        left = board[-1].edges[1] if col else None
        for candidate in boundary_index.get((mask, top, left), ()):
            if candidate.id in used:
                continue
            board.append(candidate)
            used.add(candidate.id)
            try:
                yield from search()
            finally:
                used.remove(candidate.id)
                board.pop()

    yield from search()


if __name__ == "__main__":
    demo = [
        Piece(1, (0, 1, 2, 0)),
        Piece(2, (0, 0, 3, 1)),
        Piece(3, (2, 4, 0, 0)),
        Piece(4, (3, 0, 0, 4)),
    ]
    solution = next(solve(demo), None)
    if solution is None:
        print("No solution found")
    else:
        for i in range(0, len(solution), 2):
            print([(p.id, p.edges) for p in solution[i:i + 2]])
