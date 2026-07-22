"""Control detection against live-pipeline naming: RCTLDEF… SDLC steps,
xSecurity-Gatekeeper, control JOBS, and cascade-skipped steps."""
from types import SimpleNamespace

from release_agent.tools import controls as C


def _step(name, conclusion):
    return SimpleNamespace(name=name, conclusion=conclusion, status="completed", number=1)


def _job(name, conclusion, steps):
    job = SimpleNamespace(name=name, conclusion=conclusion, status="completed", steps=steps)
    return job


def _run(jobs):
    return SimpleNamespace(jobs=lambda: jobs)


def test_is_control_step_matches_live_names():
    assert C._is_control_step("RCTLDEF0001691")
    assert C._is_control_step("rctldef0000043")  # case-insensitive
    assert C._is_control_step("xSecurity-Gatekeeper")
    assert C._is_control_step("XSECURITY-GATEKEEPER")
    assert C._is_control_step("RLFT approval gate")
    assert not C._is_control_step("Build Application")
    assert not C._is_control_step("Xray and Prisma Image Scan")  # scan ≠ control by default


def test_collect_controls_live_pipeline_shape():
    """Mirrors the real eod-deals-data-fetch run: RCTLDEF steps in a downstream
    job (one failed), xSecurity-Gatekeeper passing in build-deploy-publish, a
    control-named JOB, and cascade-skipped steps."""
    run = _run([
        _job("CodeQL Security Scan", "failure", []),  # job-level, not a control by default
        _job("build-deploy-publish", "failure", [
            _step("Create new tag", "success"),
            _step("Build Application", "success"),
            _step("xSecurity-Gatekeeper", "success"),
            _step("Veracode Scan", "skipped"),
            _step("Xray and Prisma Image Scan", "skipped"),
        ]),
        _job("publish-helm-chart", "failure", [
            _step("RCTLDEF0001033", "success"),
            _step("RCTLDEF0001691", "failure"),
            _step("RCTLDEF0000043", "failure"),
            _step("RCTLDEF0001068", "success"),
        ]),
        # a whole job named like a control (no steps reported)
        _job("RCTLDEF0000136", "success", []),
    ])
    controls = C._collect_controls(run)
    by_name = {c["control"]: c for c in controls}
    assert by_name["xSecurity-Gatekeeper"]["passed"] is True
    assert by_name["RCTLDEF0001691"]["failed"] is True
    assert by_name["RCTLDEF0000043"]["failed"] is True
    assert by_name["RCTLDEF0001033"]["passed"] is True
    # job-level control collected too
    assert by_name["RCTLDEF0000136"]["passed"] is True
    # CodeQL job is NOT a control with default prefixes
    assert "CodeQL Security Scan" not in by_name

    failed = [c["control"] for c in controls if c["failed"]]
    assert failed == ["RCTLDEF0001691", "RCTLDEF0000043"]

    # cascade-skipped steps are failures of nothing — but they keep gate != PASS
    skipped_or_other = [c for c in controls if not c["passed"] and not c["failed"]]
    assert skipped_or_other == []  # skipped scans aren't controls by default


def test_failed_steps_excludes_controls():
    run = _run([
        _job("build-deploy-publish", "failure", [
            _step("Build Application", "failure"),
            _step("RCTLDEF0001691", "failure"),
        ]),
    ])
    failed = C._failed_steps(run)
    assert [s["name"] for s in failed] == ["Build Application"]  # control reported separately
