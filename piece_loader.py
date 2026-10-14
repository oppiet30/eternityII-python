"""Load the original Eternity II piece format (north, south, west, east)."""
from pathlib import Path

from solver import Piece


def load_pieces(path: str | Path = "e2pieces.txt") -> list[Piece]:
    """Return pieces in solver order (top, right, bottom, left).

    IDs are assigned from 1 based on the source file's line order.
    """
    pieces = []
    for line_number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 4:
            raise ValueError(f"line {line_number}: expected four edge values")
        try:
            north, south, west, east = map(int, fields)
        except ValueError as exc:
            raise ValueError(f"line {line_number}: edges must be integers") from exc
        if any(value < 0 for value in (north, south, west, east)):
            raise ValueError(f"line {line_number}: edges cannot be negative")
        pieces.append(Piece(len(pieces) + 1, (north, east, south, west)))
    count = len(pieces)
    if count != 256:
        raise ValueError(f"expected 256 pieces, found {count}")
    counts = {k: sum(piece.edges.count(0) == k for piece in pieces)
              for k in (0, 1, 2)}
    if counts != {0: 196, 1: 56, 2: 4}:
        raise ValueError(f"invalid interior/border/corner counts: {counts}")
    if any(piece.edges.count(0) > 2 for piece in pieces):
        raise ValueError("a piece has more than two border edges")
    return pieces
