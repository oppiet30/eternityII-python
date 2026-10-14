from pathlib import Path

import pytest

from piece_loader import load_pieces


DATA = Path(__file__).resolve().parents[1] / "e2pieces.txt"


def test_import_original_dataset():
    pieces = load_pieces(DATA)
    assert len(pieces) == 256
    assert pieces[0].edges == (1, 17, 0, 0)
    assert len({p.id for p in pieces}) == 256
    assert sum(p.edges.count(0) == 2 for p in pieces) == 4
    assert sum(p.edges.count(0) == 1 for p in pieces) == 56
    assert sum(p.edges.count(0) == 0 for p in pieces) == 196


def test_rejects_invalid_count(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("1 0 0 17\n")
    with pytest.raises(ValueError, match="expected 256"):
        load_pieces(path)


def test_rejects_bad_columns(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("1 0 0\n")
    with pytest.raises(ValueError, match="line 1"):
        load_pieces(path)
