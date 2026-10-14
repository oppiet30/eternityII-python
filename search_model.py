"""Shared legal-placement rules for resumable search and task partitioning."""
from collections import defaultdict
from math import isqrt

from solver import Piece


class SearchModel:
    def __init__(self, pieces, size=None, fixed=()):
        self.n = isqrt(len(pieces)) if size is None else size
        if type(self.n) is not int or self.n < 1 or self.n * self.n != len(pieces):
            raise ValueError('piece count must equal size squared')
        self.pieces = {p.id: p for p in pieces}
        if len(self.pieces) != len(pieces):
            raise ValueError('piece IDs must be unique')
        if any(len(p.edges) != 4 for p in pieces):
            raise ValueError('every piece needs four edges')
        if any(type(edge) is not int or edge < 0 for p in pieces for edge in p.edges):
            raise ValueError('piece edges must be nonnegative integers')
        self.fixed = {}
        for item in fixed:
            row, col = item['row'], item['col']
            if type(row) is not int or type(col) is not int or not 0 <= row < self.n or not 0 <= col < self.n:
                raise ValueError('fixed placement outside board')
            pos = row * self.n + col
            piece = self.decode(item)
            if pos in self.fixed or piece.id in {p.id for p in self.fixed.values()}:
                raise ValueError('duplicate fixed cell or piece')
            self.fixed[pos] = piece
        self.reserved = {p.id: pos for pos, p in self.fixed.items()}
        self.by_mask = defaultdict(list)
        self.top_left = defaultdict(list)
        for piece in pieces:
            for edges in sorted({piece.rotated(k).edges for k in range(4)}):
                variant = Piece(piece.id, edges)
                mask = tuple(e == 0 for e in edges)
                self.by_mask[mask].append(variant)
                self.top_left[(mask, edges[0], edges[3])].append(variant)
        self.board, self.used = [], set()
        self.domains_cache = {}
        for pos, piece in self.fixed.items():
            if piece not in self.candidates(pos):
                raise ValueError('fixed placement violates border or neighboring fixed pieces')

    def decode(self, item):
        rotation = item.get('rotation', 0)
        if type(rotation) is not int or not 0 <= rotation < 4:
            raise ValueError('rotation must be an integer from 0 to 3')
        return self.pieces[item['piece_id']].rotated(rotation)

    def encode(self, piece):
        original = self.pieces[piece.id]
        return dict(piece_id=piece.id, rotation=next(
            k for k in range(4) if original.rotated(k) == piece))

    def candidates(self, pos=None):
        pos = len(self.board) if pos is None else pos
        row, col = divmod(pos, self.n)
        mask = (row == 0, col == self.n - 1, row == self.n - 1, col == 0)
        if not self.fixed and pos == len(self.board):
            # Keep the original indexed hot path for resumed version-1 jobs.
            top = self.board[-self.n].edges[2] if row else 0
            left = self.board[-1].edges[1] if col else 0
            return [p for p in self.top_left.get((mask, top, left), ()) if p.id not in self.used]
        requirements = {}
        for edge, neighbor, opposite, exists in (
                (0, pos - self.n, 2, row > 0), (1, pos + 1, 3, col < self.n - 1),
                (2, pos + self.n, 0, row < self.n - 1), (3, pos - 1, 1, col > 0)):
            if exists:
                other = self.board[neighbor] if neighbor < len(self.board) else self.fixed.get(neighbor)
                if other is not None:
                    requirements[edge] = other.edges[opposite]
        key = (mask, tuple(sorted(requirements.items())))
        if key not in self.domains_cache:
            self.domains_cache[key] = [p for p in self.by_mask.get(mask, ())
                                      if all(p.edges[e] == motif for e, motif in requirements.items())]
        return [p for p in self.domains_cache[key]
                if p.id not in self.used and self.reserved.get(p.id, pos) == pos
                and (pos not in self.fixed or p == self.fixed[pos])]

    def push(self, piece):
        self.board.append(piece)
        self.used.add(piece.id)

    def pop(self):
        self.used.remove(self.board.pop().id)

    def forward_possible(self):
        """Hall matching on the constrained frontier: no future tile is reused."""
        n, depth = self.n, len(self.board)
        positions = set(range(depth, min(depth + n, n * n))) | set(self.fixed)
        domains = []
        for pos in sorted(positions):
            if pos < depth:
                continue
            row, col = divmod(pos, n)
            if pos not in self.fixed and not (row and pos - n < depth or col and pos - 1 < depth):
                continue
            ids = {p.id for p in self.candidates(pos)}
            if not ids:
                return False
            domains.append(ids)
        matched = {}

        def augment(cell, seen):
            for piece_id in sorted(domains[cell]):
                if piece_id not in seen:
                    seen.add(piece_id)
                    if piece_id not in matched or augment(matched[piece_id], seen):
                        matched[piece_id] = cell
                        return True
            return False

        return all(augment(cell, set()) for cell in range(len(domains)))


def partial_score(depth, size):
    """Matched internal edges in a valid, contiguous row-major partial board."""
    return max(0, depth - size) + depth - (depth + size - 1) // size
