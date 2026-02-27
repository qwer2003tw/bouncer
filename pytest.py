#!/usr/bin/env python3
"""
python3 -m pytest 우회 방지 wrapper.
/home/ec2-user/projects/bouncer/pytest.py 에 배치하면
python3 -m pytest 실행 시 이 파일이 먼저 실행됨.
"""
import sys
import os
import subprocess

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

# 정상 실행 — 실제 pytest 호출
import runpy
# pytest가 설치된 실제 경로에서 실행
sys.exit(runpy.run_module("pytest", run_name="__main__", alter_sys=True))
