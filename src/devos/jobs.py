"""One local background worker consuming durable vault jobs."""

import logging
import threading
import time
from pathlib import Path

from devos.ports import Transport
from devos.services import IngestService
from devos.storage import SQLiteStore

log = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, path: Path, *, transport: Transport | None = None):
        self.path, self.transport = path, transport
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="devos-jobs", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def wake(self) -> None:
        self.wake_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        self.thread.join(timeout=20)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            store = None
            try:
                store = SQLiteStore(self.path)
                due = store.all("SELECT DISTINCT job_id FROM job_targets WHERE state='pending' OR (state='retry_wait' AND next_attempt_at<=?) OR (state='running' AND lease_expires<=?) LIMIT 10", (time.time(), time.time()))
                for job in due:
                    if self.stop_event.is_set():
                        break
                    IngestService(store, transport=self.transport).resume(job["job_id"])
            except Exception:
                log.error("Background job polling failed; durable jobs will be retried.")
            finally:
                if store:
                    store.close()
            self.wake_event.wait(timeout=0.5)
            self.wake_event.clear()
