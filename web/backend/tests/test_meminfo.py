"""Memory reporting, and the guard message that hid the reason a run failed.

The engine is deployed on a memory-capped instance (512 MB on the free plan)
and has been killed for exceeding it.  Nothing in the process can see that
happen, so the two things that made it invisible are pinned here: /health said
nothing about memory, and an orphaned run blamed "a restart" the planner could
not act on.
"""

import main
import meminfo


def test_report_is_json_safe_and_never_raises():
    data = meminfo.report()

    assert set(data) == {"rss_mb", "peak_rss_mb", "limit_mb"}
    for value in data.values():
        assert value is None or isinstance(value, float)


def test_an_unknown_pid_is_none_not_an_error():
    """Off Linux (dev laptops) every helper degrades to None, never raises."""
    assert meminfo.rss_mb(999_999_999) is None
    assert meminfo.peak_rss_mb(999_999_999) is None


def test_trace_prints_one_line_and_returns_the_snapshot(capsys):
    data = meminfo.trace("unit", extra=1)

    out = capsys.readouterr().out
    assert out.startswith("[mem] unit:")
    assert "extra=1" in out
    assert "limit_mb=" in out
    assert data == meminfo.report()


def test_trim_is_best_effort():
    """True/False on glibc, None elsewhere -- never an exception."""
    assert meminfo.trim() in (True, False, None)


def test_health_reports_memory(monkeypatch):
    monkeypatch.setattr(main, "_find_qgis_process", lambda: "/usr/bin/qgis_process")
    monkeypatch.setattr(main.postgis, "db_info", lambda: {})
    monkeypatch.setattr(main, "_osm_health", lambda: {})

    body = main.health()

    assert body["memory"]["limit_mb"] == meminfo.limit_mb()
    assert "rss_mb" in body["memory"]


def _failed_without_output(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(main, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main.meminfo, "limit_mb", lambda: limit)
    monkeypatch.setattr(main, "_persist_project_state", lambda *a, **k: None)
    task = main._task("proj-mem")
    task.update({"status": "running", "stage": "Resolve area"})

    main._recover_orphan_run("proj-mem", task)

    assert task["status"] == "failed"
    return task["error"]


def test_a_capped_instance_names_memory_as_the_likely_cause(tmp_path, monkeypatch):
    error = _failed_without_output(tmp_path, monkeypatch, 512.0)

    assert "512 MB" in error
    assert "memory" in error
    # Still says what to do about it.
    assert "Run it again" in error


def test_an_uncapped_instance_does_not_blame_memory(tmp_path, monkeypatch):
    error = _failed_without_output(tmp_path, monkeypatch, None)

    assert error == (
        "The engine process was killed or restarted before this run wrote any "
        "output. Run it again."
    )


def test_a_roomy_instance_does_not_blame_memory(tmp_path, monkeypatch):
    error = _failed_without_output(tmp_path, monkeypatch, 4096.0)

    assert "memory" not in error
