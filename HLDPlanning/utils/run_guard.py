"""Per-stage containment for the HLD cascade.

The cascade is a chain — objects -> polygons -> network -> trench -> chambers
-> ducts -> cables — and every stage used to be able to take the whole design
down with it.  One stage raising meant ``execute_pipeline`` aborted, the run
was reported as failed and the layers that *had* been built were never
published, even though they were already on disk and perfectly usable.

A guard wraps each stage so that a failure is contained:

  * the stage is recorded as ``failed`` with its original error,
  * the stages that depend on it are recorded as ``skipped`` with the reason,
  * the run continues, so everything else still runs and still publishes.

Nothing is silent.  ``report()`` is the machine-readable account (written next
to the design as ``run_report.json``) and it is also where the per-stage wall
clock is kept, so a slow stage can be identified from a real run instead of
guessed at.

Containment must not swallow a cancel: the caller passes ``cancel_check`` and a
stage that failed while the user was stopping the run re-raises instead of
being recorded.
"""

from __future__ import annotations

import json
import os
import time
import traceback

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

REPORT_NAME = "run_report.json"


class StageOutcome:
    """What happened to one stage of the cascade."""

    __slots__ = ("name", "status", "seconds", "error", "reason")

    def __init__(self, name, status, seconds=0.0, error="", reason=""):
        self.name = name
        self.status = status
        self.seconds = round(float(seconds), 3)
        self.error = error
        self.reason = reason

    def as_dict(self):
        out = {"name": self.name, "status": self.status, "seconds": self.seconds}
        if self.error:
            out["error"] = self.error
        if self.reason:
            out["reason"] = self.reason
        return out

    def __repr__(self):  # pragma: no cover - diagnostics only
        return "StageOutcome(%r, %r, %.3fs)" % (self.name, self.status, self.seconds)


class RunGuard:
    """Runs cascade stages so one failure cannot discard the whole design.

    ``cancel_check`` is a zero-argument callable (``feedback.isCanceled``);
    when it reports a cancel, a failed stage re-raises rather than being
    contained.
    """

    def __init__(self, cancel_check=None):
        self._cancel_check = cancel_check
        self.outcomes = []
        self._by_name = {}
        self._started = time.time()

    # ── running ──────────────────────────────────────────────────────────

    def run(self, name, fn, needs=(), feedback=None):
        """Run ``fn`` under the guard.

        Returns ``fn()`` on success, ``None`` when the stage failed or was
        skipped because something it ``needs`` did not succeed.  Never raises
        for a contained failure — except when the run is being cancelled.
        """
        blocked = [n for n in needs if self.blocked(n)]
        if blocked:
            reason = "needs %s, which did not complete" % ", ".join(blocked)
            self._record(StageOutcome(name, STATUS_SKIPPED, 0.0, reason=reason))
            self._warn(feedback, "  [skip] %s skipped: %s." % (name, reason))
            return None

        started = time.time()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - containment is the point
            if self._is_cancelled():
                raise
            elapsed = time.time() - started
            self._record(StageOutcome(
                name, STATUS_FAILED, elapsed, error=str(exc) or type(exc).__name__,
            ))
            self._warn(feedback, "  [fail] %s failed: %s" % (name, exc))
            self._warn(feedback,
                       "  [fail] The design built so far is still published; "
                       "%s and the stages after it that depend on it are reported "
                       "in %s." % (name, REPORT_NAME))
            return None
        self._record(StageOutcome(name, STATUS_OK, time.time() - started))
        return result

    def record_failure(self, name, exc):
        """Record a stage as failed when the failure is detected by the caller.

        Used where ``execute_pipeline`` validates a stage's outputs itself
        (a runner can return cleanly and still not have produced the layer the
        rest of the cascade needs).
        """
        self._record(StageOutcome(
            name, STATUS_FAILED, 0.0, error=str(exc) or type(exc).__name__,
        ))

    def record_success(self, name, seconds=0.0):
        """Record a stage that the caller ran outside ``run`` (e.g. brownfield)."""
        self._record(StageOutcome(name, STATUS_OK, seconds))

    # ── inspection ───────────────────────────────────────────────────────

    def outcome(self, name):
        return self._by_name.get(name)

    def status(self, name):
        hit = self._by_name.get(name)
        return hit.status if hit else None

    def blocked(self, name):
        """True when ``name`` did not succeed, so anything needing it must skip."""
        hit = self._by_name.get(name)
        return hit is not None and hit.status != STATUS_OK

    def failed(self):
        return [o.name for o in self.outcomes if o.status == STATUS_FAILED]

    def skipped(self):
        return [o.name for o in self.outcomes if o.status == STATUS_SKIPPED]

    def is_clean(self):
        return not self.failed() and not self.skipped()

    # ── report ───────────────────────────────────────────────────────────

    def report(self):
        stages = [o.as_dict() for o in self.outcomes]
        # Slowest first: the point of the timing is to name the stage worth
        # optimising without having to read the whole log.
        slowest = sorted(
            (o for o in self.outcomes if o.status == STATUS_OK),
            key=lambda o: o.seconds, reverse=True,
        )
        return {
            "ok": self.is_clean(),
            "stages": stages,
            "failed": self.failed(),
            "skipped": self.skipped(),
            "slowest_stages": [
                {"name": o.name, "seconds": o.seconds} for o in slowest[:5]
            ],
            "total_stage_seconds": round(
                sum(o.seconds for o in self.outcomes), 3
            ),
            "elapsed_seconds": round(time.time() - self._started, 3),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def write_report(self, out_dir, filename=REPORT_NAME, extra=None):
        """Write the report beside the design.  Returns the path, or ``None``.

        Never raises: a run must not fail because its own bookkeeping could not
        be written.  The file is replaced atomically so a reader never sees a
        half-written report.
        """
        if not out_dir:
            return None
        path = os.path.join(out_dir, filename)
        payload = self.report()
        if extra:
            payload.update(extra)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return None
        return path

    # ── internals ────────────────────────────────────────────────────────

    def _is_cancelled(self):
        if self._cancel_check is None:
            return False
        try:
            return bool(self._cancel_check())
        except Exception:
            return False

    def _record(self, outcome):
        """Keep one outcome per stage: a later record for the same stage replaces it.

        A stage is recorded twice when the caller validates its outputs after
        the runner returned (a runner can complete and still produce nothing
        usable), so the time spent is added rather than lost — ``seconds`` is
        total time in that stage, which is what the timing report is for.
        """
        previous = self._by_name.get(outcome.name)
        if previous is not None:
            outcome.seconds = round(previous.seconds + outcome.seconds, 3)
            try:
                self.outcomes[self.outcomes.index(previous)] = outcome
            except ValueError:
                self.outcomes.append(outcome)
        else:
            self.outcomes.append(outcome)
        self._by_name[outcome.name] = outcome

    @staticmethod
    def _warn(feedback, message):
        if feedback is None:
            return
        try:
            feedback.pushWarning(message)
        except Exception:
            pass


def summarise(guard, feedback=None):
    """Log the guard's verdict: one line naming what failed, or all-clear."""
    failed, skipped = guard.failed(), guard.skipped()
    if not failed and not skipped:
        if feedback is not None:
            try:
                feedback.pushInfo("  [stages] All stages completed.")
            except Exception:
                pass
        return
    parts = []
    if failed:
        parts.append("failed: " + ", ".join(failed))
    if skipped:
        parts.append("skipped: " + ", ".join(skipped))
    if feedback is not None:
        try:
            feedback.pushWarning("  [stages] Incomplete design — " + "; ".join(parts))
        except Exception:
            pass


def format_exception(exc):
    """The traceback tail, for the report's ``error`` field when it matters."""
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()
