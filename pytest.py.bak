#!/usr/bin/env python3
"""
OOM Guard: python3 -m pytest tests/ 전체 실행 차단.
tests/ 특정 파일 지정은 허용.
"""
import sys
import os

BOUNCER_REPO = "/home/ec2-user/projects/bouncer"
cwd = os.getcwd()

if cwd.startswith(BOUNCER_REPO):
    args = sys.argv[1:]
    for arg in args:
        if arg in ("tests/", "tests", "tests/."):
            print("⚠️  [OOM Guard] 전체 pytest suite 감지됨!", file=sys.stderr)
            print(f"    전체 suite: bash {BOUNCER_REPO}/scripts/run-tests.sh --all", file=sys.stderr)
            print(f"    특정 모듈: bash {BOUNCER_REPO}/scripts/run-tests.sh tests/test_foo.py", file=sys.stderr)
            sys.exit(1)

# 재귀 방지: pytest.py를 sys.path에서 제거 후 실제 pytest 실행
sys.path = [p for p in sys.path if not p == BOUNCER_REPO]

from pytest import main
sys.exit(main())
