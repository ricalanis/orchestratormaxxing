#!/usr/bin/env python3
import json
import sys
import time

json.load(sys.stdin)
time.sleep(5)
json.dump({"output": "PONG", "usage": {}, "events": []}, sys.stdout)
