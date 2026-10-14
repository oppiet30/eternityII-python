from solver import Piece, solve


def test_single_piece_all_border():
    assert list(solve([Piece(42, (0, 0, 0, 0))])) == [
        (Piece(42, (0, 0, 0, 0)),)
    ]


def test_rotated_scrambled_board():
    pieces = [
        Piece(4, (3, 0, 0, 4)).rotated(1),
        Piece(2, (0, 0, 3, 1)).rotated(2),
        Piece(1, (0, 1, 2, 0)).rotated(3),
        Piece(3, (2, 4, 0, 0)).rotated(1),
    ]
    result = next(solve(pieces), None)
    assert result is not None
    for i, piece in enumerate(result):
        row, col = divmod(i, 2)
        if row:
            assert result[i - 2].edges[2] == piece.edges[0]
        if col:
            assert result[i - 1].edges[1] == piece.edges[3]


def test_unsatisfiable_border():
    pieces = [Piece(i, (1, 1, 1, 1)) for i in range(4)]
    assert list(solve(pieces)) == []
