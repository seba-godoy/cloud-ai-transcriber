#!/usr/bin/env python3
"""Explicitly requeue one guarded Firestore permanent failure using ADC."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state_backend import FirestoreStateBackend, RecoveryRefusedError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_key", help="Exact transcription_jobs document ID")
    parser.add_argument("--project", required=True, help="Google Cloud project ID")
    parser.add_argument("--database", default="(default)", help="Firestore database ID")
    parser.add_argument("--expected-error", required=True,
                        help="Case-insensitive fingerprint required in existing error evidence")
    parser.add_argument("--reason", default="operator_unicode_filename_recovery",
                        help="Non-secret audit reason")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    backend = FirestoreStateBackend(project_id=args.project, database_id=args.database)
    try:
        backend.requeue_permanent_failure(args.source_key, args.expected_error, args.reason)
    except RecoveryRefusedError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(f"Requeued {args.source_key}; preserved chunks, outputs, notifications, and error evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
