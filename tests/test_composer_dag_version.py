"""Bumping the DF template version inside a Composer DAG.

The version is the fallback of a Jinja expression embedded in a GCS path:

    "gs://<bucket>/templates/<image>/{{dag_run.conf['version'] | default('0.0.494')}}/dataflow_job.json"

Only that literal may change. The bucket, the image path and the conf override
(which lets an operator pin a version for one DAG run) all have to survive, so
these tests are mostly about what must NOT move.
"""
import pytest

from release_agent.tools import composer as C

# The real shape, with the org's identifiers replaced by generic ones.
DAG = '''from airflow import DAG
from airflow.providers.google.cloud.operators.dataflow import DataflowStartFlexTemplateOperator

with DAG("acme-svc-alpha", schedule=None, catchup=False) as dag:
    start = DataflowStartFlexTemplateOperator(
        task_id="acme-svc-alpha",
        body={
            "launchParameter": {
                "containerSpecGcsPath": "gs://uat-acme-test-data/templates/acme-svc/{{dag_run.conf['version'] | default('0.0.494')}}/dataflow_job.json",
                "environment": {
                    "machineType": "e2-standard-4",
                    "ipConfiguration": "WORKER_IP_PRIVATE",
                },
            }
        },
    )
'''


def test_reads_the_current_version():
    assert C.current_versions(DAG) == ["0.0.494"]


def test_replaces_only_the_version_characters():
    out, replaced = C.set_default_version(DAG, "0.0.495")
    assert replaced == ["0.0.494"]
    assert "default('0.0.495')" in out
    # everything around it survives verbatim
    assert "gs://uat-acme-test-data/templates/acme-svc/" in out
    assert "dag_run.conf['version']" in out          # the per-run override
    assert "/dataflow_job.json" in out
    assert out.count("\n") == DAG.count("\n")        # no line added or lost
    # and the ONLY textual difference is the version
    assert out.replace("0.0.495", "0.0.494") == DAG


def test_double_quoted_conf_key_and_value_are_handled():
    text = '''p = "gs://b/t/{{dag_run.conf["version"] | default("1.2.3")}}/x.json"'''
    out, replaced = C.set_default_version(text, "9.9.9")
    assert replaced == ["1.2.3"]
    assert 'default("9.9.9")' in out


def test_several_operators_in_one_dag_all_move():
    text = DAG + DAG.replace("0.0.494", "0.0.400")
    out, replaced = C.set_default_version(text, "1.0.0")
    assert replaced == ["0.0.494", "0.0.400"]        # reported in file order
    assert C.current_versions(out) == ["1.0.0", "1.0.0"]


def test_an_unrelated_default_filter_is_not_touched():
    """Only a default() belonging to the version lookup qualifies — a DAG full of
    other Jinja fallbacks must come through unchanged."""
    text = (
        'region = "{{dag_run.conf[\'region\'] | default(\'europe-west3\')}}"\n'
        'subnet = "{{var.value.subnet | default(\'sn-default\')}}"\n'
        + DAG
    )
    out, replaced = C.set_default_version(text, "2.0.0")
    assert replaced == ["0.0.494"]
    assert "default('europe-west3')" in out
    assert "default('sn-default')" in out


def test_a_dag_with_no_version_fallback_is_refused():
    """Silently changing nothing would let the deploy look complete while the
    DAG still launched the old template."""
    with pytest.raises(C.DagVersionNotFound):
        C.set_default_version('x = "gs://b/t/fixed/dataflow_job.json"', "1.0.0")


def test_a_conf_lookup_without_a_default_is_not_a_match():
    with pytest.raises(C.DagVersionNotFound):
        C.set_default_version('''p = "{{dag_run.conf['version']}}"''', "1.0.0")


def test_a_default_outside_the_expression_is_not_a_match():
    """The default() has to sit inside the same {{ }} as the lookup."""
    text = '''a = "{{dag_run.conf['version']}}"\nb = "{{other | default('nope')}}"'''
    with pytest.raises(C.DagVersionNotFound):
        C.set_default_version(text, "1.0.0")


def test_bumping_to_the_same_version_is_reported_not_hidden():
    out, replaced = C.set_default_version(DAG, "0.0.494")
    assert replaced == ["0.0.494"]
    assert out == DAG


# ------------------------------------------------- preview inside the deploy

def test_the_dag_bump_is_previewed_with_the_dispatch(monkeypatch):
    """Approving a DF deploy must mean approving BOTH mutations, so the DAG
    diff has to be in the same preview as the dispatch inputs — not discovered
    afterwards."""
    from adk_release_agent import deploy as D
    from release_agent.config import settings

    monkeypatch.setattr(settings, "df_dispatch_inputs",
                        '{"module": "{image}", "binary_version": "{tag}"}')
    monkeypatch.setattr(settings, "df_deploy_workflow", "UAT.yaml")
    monkeypatch.setattr(
        C, "preview_dag_bump",
        lambda files, version, env, repo="": {
            "repo": "acme/composer-dags", "branch": "main", "dir": "uat",
            "changes": [{"file": "uat/acme-svc-beta.py", "from": ["0.0.494"],
                         "to": version, "unchanged": False}],
            "problems": [{"file": "uat/sql-query.py",
                          "error": "no dag_run.conf['version'] default(...) in this file"}],
        },
    )

    preview = D._build_preview({
        "deployment_type": "dataflow", "environment": "uat",
        "images": [{"name": "acme-svc", "tag": "0.0.495"}],
        "dag_files": ["acme-svc-beta.py", "sql-query.py"],
    })

    assert preview["workflow_dispatch (UAT.yaml)"] == [
        {"module": "acme-svc", "binary_version": "0.0.495"}]
    dag_section = next(v for k, v in preview.items() if k.startswith("Composer DAG bump"))
    assert {"file": "uat/acme-svc-beta.py", "version": "0.0.494 → 0.0.495"} in dag_section
    # a file with no version fallback is named, not silently dropped
    assert any("PROBLEM" in row["version"] for row in dag_section)


def test_no_dag_files_means_no_dag_section(monkeypatch):
    """The bump is opt-in; a plain dispatch preview must not grow a section."""
    from adk_release_agent import deploy as D

    preview = D._build_preview({
        "deployment_type": "dataflow", "environment": "uat",
        "images": [{"name": "acme-svc", "tag": "0.0.495"}],
    })
    assert not any(k.startswith("Composer DAG bump") for k in preview)


def test_a_named_repo_overrides_the_configured_one(monkeypatch):
    """Teams whose DAGs are not all in one repo name it per deploy; empty falls
    back to COMPOSER_REPO."""
    from release_agent.config import settings

    monkeypatch.setattr(settings, "composer_repo", "acme/default-dags")
    assert C._resolve_repo("") == "acme/default-dags"
    assert C._resolve_repo("  ") == "acme/default-dags"
    assert C._resolve_repo("acme/other-dags") == "acme/other-dags"


def test_the_override_reaches_github_and_the_preview_reports_it(monkeypatch):
    from release_agent.config import settings

    monkeypatch.setattr(settings, "composer_repo", "acme/default-dags")
    monkeypatch.setattr(settings, "composer_branch", "main")
    asked = []

    class _Blob:
        decoded_content = DAG.encode()

    class _Repo:
        def get_contents(self, path, ref):
            return _Blob()

    def _client():
        return type("C", (), {"get_repo": staticmethod(
            lambda full: asked.append(full) or _Repo())})()

    monkeypatch.setattr(C, "_get_github_client", _client)
    out = C.preview_dag_bump(["acme-svc-alpha.py"], "0.0.495", "uat", repo="acme/other-dags")
    assert asked == ["acme/other-dags"]
    assert out["repo"] == "acme/other-dags"
    assert out["changes"][0]["from"] == ["0.0.494"]


def test_the_payload_carries_the_named_repo():
    import json

    from release_agent.agent.parsing import _try_parse_json_payload

    req = _try_parse_json_payload(json.dumps({
        "deployment_type": "dataflow", "environment": "uat",
        "image": "acme-svc", "tag": "0.0.495",
        "dag_files": ["acme-svc-alpha.py"], "composer_repo": " acme/other-dags ",
    }))
    assert req["composer_repo"] == "acme/other-dags"


def test_no_change_leaves_no_branch_behind(monkeypatch):
    """The branch is created before we know whether anything will change, so a
    no-op has to clean up after itself — otherwise every attempt silts a
    dag-version/* branch into the repo that looks like a half-finished bump."""
    from release_agent.config import settings

    monkeypatch.setattr(settings, "composer_repo", "acme/dags")
    monkeypatch.setattr(settings, "composer_branch", "main")
    deleted = []

    class _Ref:
        object = type("O", (), {"sha": "base-sha"})()

        def delete(self):
            deleted.append(True)

    class _Repo:
        def get_branch(self, name):
            return type("B", (), {"commit": type("C", (), {"sha": "base-sha"})()})()

        def create_git_ref(self, ref, sha):
            return None

        def get_contents(self, path, ref):
            # already on the target version -> nothing to change
            return type("Blob", (), {
                "decoded_content": DAG.replace("0.0.494", "0.0.500").encode(),
                "sha": "blob-sha",
            })()

        def get_git_ref(self, ref):
            return _Ref()

        def create_pull(self, **kw):
            raise AssertionError("no PR should be opened when nothing changed")

    monkeypatch.setattr(
        C, "_get_github_client",
        lambda: type("C", (), {"get_repo": staticmethod(lambda full: _Repo())})(),
    )
    out = C.apply_dag_bump(["acme-svc-alpha.py"], "0.0.500", "uat")
    assert out["action"] == "dag_bump_not_needed"
    assert out["branch"] == ""          # nothing for the caller to link to
    assert deleted == [True]
