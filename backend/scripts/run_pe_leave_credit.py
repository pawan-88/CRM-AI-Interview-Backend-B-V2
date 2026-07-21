#!/usr/bin/env python3
"""CLI: run Project Employee monthly leave credit (+ year-end carry/expiry).

Usage:
  cd backend
  python scripts/run_pe_leave_credit.py
  python scripts/run_pe_leave_credit.py --as-of 2026-07-01
  python scripts/run_pe_leave_credit.py --pe-id 12 --as-of 2026-07-01

Cron example (1st of month 02:00):
  0 2 1 * * cd /path/to/backend && python scripts/run_pe_leave_credit.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# Allow `python scripts/run_pe_leave_credit.py` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crm_db import get_session_factory  # noqa: E402
from services.project_employee_leave_credit import run_pe_leave_credit  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Credit PE leave for the period")
    parser.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--pe-id", type=int, default=None, help="Limit to one project_employee id")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    db = get_session_factory()()
    try:
        summary = run_pe_leave_credit(db, as_of=as_of, pe_id=args.pe_id)
    finally:
        db.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
