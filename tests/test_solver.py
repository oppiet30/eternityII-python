import pytest

from solver import Piece, solve


def test_clockwise_rotation():
    p = Piece(7, (1, 2, 3, 4))
    assert p.rotated(1).edges == (4, 1, 2, 3)
    assert p.rotated(4) == p


def test_two_by_two_solution():
    pieces = [
        Piece(1, (0, 1, 2, 0)),
        Piece(2, (0, 0, 3, 1)),
        Piece(3, (2, 4, 0, 0)),
        Piece(4, (3, 0, 0, 4)),
    ]
    board = next(solve(pieces), None)
    assert board is not None
    assert {p.id for p in board} == {1, 2, 3, 4}
    for index, piece in enumerate(board):
        row, col = divmod(index, 2)
        t, r, b, l = piece.edges
        assert (t == 0) == (row == 0)
        assert (r == 0) == (col == 1)
        assert (b == 0) == (row == 1)
        assert (l == 0) == (col == 0)
        if row:
            assert board[index - 2].edges[2] == t
        if col:
            assert board[index - 1].edges[1] == l


def test_invalid_piece_count():
    with pytest.raises(ValueError):
        list(solve([Piece(1, (0, 0, 0, 0)), Piece(2, (0, 0, 0, 0))]))


def test_duplicate_ids():
    with pytest.raises(ValueError):
        list(solve([Piece(1, (0, 0, 0, 0))] * 4))


def test_impossible_board():
    pieces = [Piece(i, (1, 1, 1, 1)) for i in range(4)]
    assert list(solve(pieces)) == []
