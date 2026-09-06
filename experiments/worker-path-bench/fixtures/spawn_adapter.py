#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
import signal

case = json.load(sys.stdin)
marker = case["marker"]
pidfile = case["pidfile"]
open(pidfile, "w").write(str(os.getpid()))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([
    sys.executable, "-c",
    "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('orphan')",
    marker,
])
time.sleep(5)
