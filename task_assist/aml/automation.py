"""AML 风险任务自动扫描与定时提交。"""

from __future__ import annotations

import threading
import time
import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING, Callable

from task_assist import config
from task_assist.infrastructure.ledger import SubmissionLedger
from .service import TaskRejected

if TYPE_CHECKING:
    from .service import TaskService
    from task_assist.infrastructure.state_store import StateStore
    from task_assist.infrastructure.telemetry import Trace


class AutoSubmitter:
    def __init__(self, service_factory: Callable[[Trace], TaskService], ledger: SubmissionLedger,
                 audit_store: StateStore | None = None, lock_provider=None):
        self.service_factory = service_factory
        self.ledger = ledger
        self.audit_store = audit_store
        self.lock_provider = lock_provider
        self._run_lock = threading.Lock()

    def run_once(self, trace: Trace, limit: int = config.MAX_AUTO_SUBMIT_PER_RUN) -> dict:
        # 多实例只允许一个扫描器推进；提交幂等记录仍是最终防线。
        lock = self.lock_provider("auto_submit") if self.lock_provider else nullcontext()
        with lock:
            return self._run_locked(trace, limit)

    def _run_locked(self, trace: Trace, limit: int) -> dict:
        if not self._run_lock.acquire(blocking=False):
            raise TaskRejected("已有自动提交任务正在执行")
        try:
            with trace.step("automation", "scan_and_submit"):
                service = self.service_factory(trace)
                try:
                    trace.owner_id = service.actor.user_id
                    summary = service.summary(trace)
                    risk_count = sum(item["count"] for item in summary["by_type"] if item["process_type"] == "risk")
                    # 先取快照，再执行提交，避免页码随任务完成而移动。
                    candidates = []
                    page = 1
                    detail_total = risk_count
                    while risk_count > 0 and page <= 50:
                        batch = service.risk_page(trace, page, 100)
                        rows = batch["records"]
                        detail_total = int(batch["total"])
                        candidates.extend(rows)
                        if not rows or page * 100 >= detail_total:
                            break
                        page += 1

                    result = {
                        "risk_count": risk_count,
                        "scanned": len(candidates),
                        "submitted": [],
                        "skipped": [],
                        "errors": [],
                        "uncertain": [],
                        "remaining_unscanned": max(0, detail_total - len(candidates)),
                        "deferred_due_to_limit": 0,
                    }
                    seen = set()
                    for row in candidates:
                        task_id, act_id = row.get("task_id"), row.get("act_id")
                        if not task_id or task_id in seen:
                            continue
                        seen.add(task_id)
                        if len(result["submitted"]) >= limit:
                            result["deferred_due_to_limit"] += 1
                            continue
                        grade = row["risk_level"]
                        if not grade["can_one_click"]:
                            result["skipped"].append({"task_id": task_id, "reason": grade["reason"]})
                            continue
                        existing = self.ledger.status(task_id)
                        if existing:
                            result["skipped"].append({"task_id": task_id, "reason": f"本地提交记录: {existing}"})
                            continue
                        try:
                            submitted = service.approve_risk(trace, task_id, act_id, ledger=self.ledger)
                            result["submitted"].append(submitted)
                            if self.audit_store:
                                self.audit_store.audit(service.actor.user_id, "auto_approve", task_id, "submitted", trace.id)
                        except TaskRejected as exc:
                            result["skipped"].append({"task_id": task_id, "reason": str(exc)})
                        except Exception as exc:
                            # 提交前错误可在下轮重试；提交调用异常标记为 uncertain 后不再重试。
                            bucket = "uncertain" if self.ledger.status(task_id) == "uncertain" else "errors"
                            result[bucket].append({"task_id": task_id, "error_type": type(exc).__name__})
                            if self.audit_store:
                                self.audit_store.audit(service.actor.user_id, "auto_approve", task_id, bucket, trace.id)
                    trace.record("automation", "run_summary",
                                 scanned=result["scanned"], submitted=len(result["submitted"]),
                                 skipped=len(result["skipped"]), errors=len(result["errors"]),
                                 uncertain=len(result["uncertain"]))
                    return result
                finally:
                    close = getattr(service.backend, "close", None)
                    if close:
                        close()
        finally:
            self._run_lock.release()


class AutoSubmitScheduler:
    def __init__(self, submitter: AutoSubmitter, trace_store, interval: int):
        self.submitter = submitter
        self.trace_store = trace_store
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="aml-auto-submit", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self) -> None:
        # 启动后立即执行，以后按间隔轮询。
        while not self.stop_event.is_set():
            from task_assist.infrastructure.telemetry import Trace

            trace = Trace("scheduled_auto_submit")
            try:
                self.submitter.run_once(trace)
            except Exception as exc:
                trace.record("automation", "run_once", "error", error_type=type(exc).__name__)
            finally:
                try:
                    self.trace_store.save(trace)
                except Exception as exc:
                    # 观测库故障不应永久停止后台扫描线程。
                    logging.getLogger(__name__).error("定时扫描 Trace 保存失败: %s", type(exc).__name__)
            self.stop_event.wait(self.interval)
