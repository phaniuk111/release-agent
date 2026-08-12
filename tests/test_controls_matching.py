"""Control detection against live-pipeline naming: RCTLDEF… SDLC steps,
control JOBS, and cascade-skipped steps.

RCTLD is the live gate. Scanner steps that run in the same pipeline —
xSecurity-Gatekeeper, Xray/Prisma, CodeQL, Veracode — are deliberately NOT
controls: each configured prefix can REFUSE a queue request, so gating on a
check the release sign-off does not depend on would block eligible builds."""
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
    assert C._is_control_step("RCTLD-1234 Approval")
    assert C._is_control_step("RLFT approval gate")
    assert not C._is_control_step("Build Application")
    # scanners are ordinary steps, not release controls
    assert not C._is_control_step("xSecurity-Gatekeeper")
    assert not C._is_control_step("Xray and Prisma Image Scan")
    assert not C._is_control_step("CodeQL Security Scan")


def test_collect_controls_live_pipeline_shape():
    """Mirrors a real pipeline run: RCTLDEF steps in a downstream job (one
    failed), scanner steps that must NOT be counted as controls, a control-named
    JOB, and cascade-skipped steps."""
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
    assert by_name["RCTLDEF0001691"]["failed"] is True
    assert by_name["RCTLDEF0000043"]["failed"] is True
    assert by_name["RCTLDEF0001033"]["passed"] is True
    # job-level control collected too
    assert by_name["RCTLDEF0000136"]["passed"] is True
    # scanners are NOT controls with the default prefixes — a passing scanner
    # must not make the gate look complete, nor a failing one refuse the queue
    assert "CodeQL Security Scan" not in by_name
    assert "xSecurity-Gatekeeper" not in by_name
    assert "Xray and Prisma Image Scan" not in by_name

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


def test_control_names_are_stripped_before_matching():
    """A stray leading space would otherwise un-match a control, and an
    unmatched control is indistinguishable from a run that has none — the build
    would read as clean while an SDLC gate went unchecked."""
    assert C._is_control_step(" RCTLDEF0001691")
    assert C._is_control_step("RCTLDEF0001691 ")
    assert C._is_control_step("\tRCTLDEF0001691")


def test_report_separates_failed_from_open_controls():
    """The two need different words to the developer: a failure means fix and
    re-run, an open control means wait (or chase it)."""
    run = _run([
        _job("publish-helm-chart", "failure", [
            _step("RCTLDEF0001033", "success"),
            _step("RCTLDEF0001691", "failure"),
            _step("RCTLDEF0000043", "skipped"),
            _step("RCTLDEF0001068", None),          # never ran
        ]),
    ])
    controls = C._collect_controls(run)
    failed = [c for c in controls if c["failed"]]
    open_ = [c for c in controls if not c["passed"] and not c["failed"]]

    assert [c["control"] for c in failed] == ["RCTLDEF0001691"]
    assert [c["control"] for c in open_] == ["RCTLDEF0000043", "RCTLDEF0001068"]
    # the job is carried so the dev knows where to look
    assert failed[0]["job"] == "publish-helm-chart"
