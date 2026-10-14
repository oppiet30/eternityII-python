"""Constraint-based edge-matching puzzle solver.

Edges are ordered (top, right, bottom, left). 0 denotes the outer border.
"""
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


def solve(pieces: list[Piece], size: int | None = None):
    """Yield complete boards, row-major, using each piece once.

    Intended for small puzzles; exhaustive search on a 16x16 board
    requires additional pruning and distributed search.
    """
    n = isqrt(len(pieces)) if size is None else size
    if n < 1 or n * n != len(pieces):
        raise ValueError("piece count must equal size squared")
    if len({p.id for p in pieces}) != len(pieces):
        raise ValueError("piece IDs must be unique")
    if any(len(p.edges) != 4 for p in pieces):
        raise ValueError("every piece needs four edges")
    board = []
    used = set()

    def search():
        pos = len(board)
        if pos == n * n:
            yield tuple(board)
            return
        row, col = divmod(pos, n)
        for piece in pieces:
            if piece.id in used:
                continue
            orientations = {piece.rotated(k).edges for k in range(4)}
            for edges in sorted(orientations):
                top, right, bottom, left = edges
                if (top == 0) != (row == 0):
                    continue
                if (left == 0) != (col == 0):
                    continue
                if (bottom == 0) != (row == n - 1):
                    continue
                if (right == 0) != (col == n - 1):
                    continue
                if row and board[pos - n].edges[2] != top:
                    continue
                if col and board[-1].edges[1] != left:
                    continue
                board.append(Piece(piece.id, edges))
                used.add(piece.id)
                yield from search()
                used.remove(piece.id)
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
