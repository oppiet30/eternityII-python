"""Prepare disjoint search batches; default is a reviewable SQL file."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

from piece_loader import load_pieces
from search_model import SearchModel


DATA = Path(__file__).with_name('e2pieces.txt')
# Dataset orientations differ from other published encodings. In this file,
# the mandatory tile faces (top,right,bottom,left)=(18,6,6,11).
# Zero-based row 8, column 7; see README's starter-piece provenance.
OFFICIAL_STARTER = dict(row=8, col=7, piece_id=139, rotation=2)


def puzzle_fingerprint(pieces):
    return hashlib.sha256(json.dumps(
        [(p.id, p.edges) for p in sorted(pieces, key=lambda p: p.id)],
        separators=(',', ':')).encode()).hexdigest()


def search_policy(pieces, parameters, legacy=False):
    if legacy:
        return [], False, False
    bundled = pieces == load_pieces(DATA)
    official = parameters.get('official_starter', bundled)
    if type(official) is not bool:
        raise ValueError('official_starter must be boolean')
    fixed = list(parameters.get('fixed', []))
    if official:
        if not bundled or parameters.get('size', 16) != 16:
            raise ValueError('official starter requires the bundled 16x16 dataset')
        if any(p.get('piece_id') == 139 or (p.get('row'), p.get('col')) == (8, 7) for p in fixed):
            if OFFICIAL_STARTER not in fixed:
                raise ValueError('fixed placement conflicts with mandatory starter')
        else:
            fixed.append(dict(OFFICIAL_STARTER))
    forward = parameters.get('forward_check', True)
    if type(forward) is not bool:
        raise ValueError('forward_check must be boolean')
    return fixed, forward, official


def partition(pieces, size=None, depth=1, prefix=(), fixed=(), max_jobs=10000, forward_check=True):
    model = SearchModel(pieces, size, fixed)
    if type(depth) is not int or not len(prefix) < depth <= model.n * model.n:
        raise ValueError('depth must exceed prefix length and fit the board')
    for piece in prefix:
        if piece not in model.candidates():
            raise ValueError('invalid base prefix')
        model.push(piece)
    leaves = []

    def visit():
        if len(model.board) == depth:
            if len(leaves) >= max_jobs:
                raise ValueError('partition exceeds max_jobs; choose a smaller depth')
            leaves.append([model.encode(p) for p in model.board])
            return
        for piece in model.candidates():
            model.push(piece)
            if not forward_check or model.forward_possible():
                visit()
            model.pop()

    visit()
    return leaves


def sql_literal(value):
    # UTF-8 hex avoids SQL-mode-dependent quote/backslash escaping.
    return "CONVERT(X'" + json.dumps(value, separators=(',', ':')).encode().hex() + "' USING utf8mb4)"


def batch_sql(prefixes, parameters, base_prefix=(), source_job=None, batch_id=None):
    if not prefixes:
        raise ValueError('no viable prefixes; no batch will be queued')
    batch_id = batch_id or str(uuid.uuid4())
    # UUID is generated locally; never interpolate arbitrary caller text.
    batch_id = str(uuid.UUID(batch_id))
    if source_job is not None and (type(source_job) is not int or source_job < 1):
        raise ValueError('source job ID must be a positive integer')
    source = 'NULL' if source_job is None else str(source_job)
    return ("-- Review this batch before importing. A repeated batch ID is a no-op.\n"
            "-- Replacement explicitly cancels only the indicated source job.\n"
            "USE eternity2;\n"
            f"CALL proc_enqueue_partition_batch('{batch_id}', {sql_literal(prefixes)}, "
            f"{sql_literal(parameters)}, {sql_literal(list(base_prefix))}, {source});\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--depth', type=int, default=1)
    parser.add_argument('--pieces-file', type=Path, default=DATA)
    parser.add_argument('--parameters', default='{}', help='Job parameters as JSON')
    parser.add_argument('--prefix', default='[]', help='Existing prefix to subdivide, as JSON')
    parser.add_argument('--replace-job', type=int, help='Explicitly retire this source job when SQL is imported')
    parser.add_argument('--max-jobs', type=int, default=10000)
    parser.add_argument('--output', type=Path, default=Path('partition-batch.sql'))
    args = parser.parse_args()
    parameters = json.loads(args.parameters)
    if not isinstance(parameters, dict):
        parser.error('--parameters must be a JSON object')
    if args.pieces_file.resolve() != DATA.resolve() and not parameters.get('pieces_file'):
        parser.error('custom --pieces-file also requires a worker-side parameters.pieces_file path')
    pieces = load_pieces(args.pieces_file)
    # File fingerprints are portable; do not embed machine-specific absolute paths.
    parameters['pieces_sha256'] = hashlib.sha256(args.pieces_file.read_bytes()).hexdigest()
    parameters['puzzle_fingerprint'] = puzzle_fingerprint(pieces)
    fixed, forward, official = search_policy(pieces, parameters)
    parameters['official_starter'] = official
    encoded_prefix = json.loads(args.prefix)
    model = SearchModel(pieces, parameters.get('size'), fixed)
    prefix = [model.decode(p) for p in encoded_prefix]
    leaves = partition(pieces, parameters.get('size'), args.depth, prefix, fixed,
                       args.max_jobs, forward)
    args.output.write_text(batch_sql(leaves, parameters, encoded_prefix, args.replace_job), encoding='utf-8')
    print(f'Prepared {len(leaves)} disjoint jobs in {args.output}; no database changes made.')


if __name__ == '__main__':
    main()
