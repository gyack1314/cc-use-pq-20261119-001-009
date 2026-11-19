from __future__ import annotations

import json
import signal
import threading

from frost.domain import Fault, bounded_int


class Worker:
    def __init__(self, service, poll_seconds: int = 5, batch_size: int = 10, output=None):
        bounded_int(poll_seconds, "调度唤醒间隔", 1, 300)
        bounded_int(batch_size, "调度批量", 1, 100)
        self.service = service
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.output = output or print
        self.stopped = threading.Event()

    def stop(self, *_):
        self.stopped.set()

    def run(self, once=False):
        totals = {"cycles": 0, "complete": 0, "unchanged": 0, "failed": 0, "abandoned": 0}
        while not self.stopped.is_set():
            result = self.service.run_due(self.batch_size)
            totals["cycles"] += 1
            for run in result["runs"]:
                state = run["state"]
                if state in totals:
                    totals[state] += 1
                self.output(json.dumps(run, ensure_ascii=False, sort_keys=True))
            if once:
                break
            self.stopped.wait(self.poll_seconds)
        return totals

    def with_signals(self, once=False):
        previous = {}
        try:
            for name in ("SIGINT", "SIGTERM"):
                value = getattr(signal, name, None)
                if value is not None:
                    previous[value] = signal.signal(value, self.stop)
            return self.run(once)
        finally:
            for value, handler in previous.items():
                signal.signal(value, handler)
