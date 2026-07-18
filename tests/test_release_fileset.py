"""Live release model: payload validation, env partition, parsing, promotion."""
import json
from types import SimpleNamespace

from release_agent.agent.parsing import _try_parse_json_payload
from release_agent.tools import release_fileset as RF

BASE = "https://artifactory.example/com/db/acme-ds"


def _payload(**over):
    base = {
        "release_name": "July 20th 2026 : Release 31",
        "start_date": "2026-07-20 10:00:00",
        "end_date": "2026-07-21 23:00:00",
        "change_initiator": "dev@db.com",
        "change_summary": "Release 31",
        "prl1_only": ["acme-risk-fetcher"],
        "df_images": [],
        "artefact": [
            f"{BASE}/acme-workflow-service:4.0.66",
            f"{BASE}/acme-risk-fetcher:4.0.153",
            f"{BASE}/acme-capability-svc:1.2.3",
        ],
    }
    base.update(over)
    return base


def test_validate_release_happy_path(monkeypatch):
    monkeypatch.setattr(RF.settings, "artifactory_base_url", BASE + "/", raising=False)
    details, errors = RF.validate_release(_payload(artefact=["acme-workflow-service:4.0.66"], prl1_only=[]))
    assert errors == []
    # bare name:version normalized to a full URL
    assert details["artefact"] == [f"{BASE}/acme-workflow-service:4.0.66"]


def test_validate_release_hard_failures():
    _, errors = RF.validate_release(_payload(prl1_only=["typo-svc"]))
    assert any("typo-svc" in e for e in errors)
    _, errors = RF.validate_release(_payload(start_date="20/07/2026 10:00"))
    assert any("YYYY-MM-DD" in e for e in errors)
    _, errors = RF.validate_release(_payload(end_date="2026-07-20 09:00:00"))
    assert any("after" in e for e in errors)
    _, errors = RF.validate_release(_payload(change_initiator=""))
    assert any("change_initiator" in e for e in errors)
    dup = _payload()
    dup["artefact"].append(f"{BASE}/acme-workflow-service:9.9.9")
    _, errors = RF.validate_release(dup)
    assert any("Duplicate" in e for e in errors)


def test_partition_mirrors_script_rules():
    details, errors = RF.validate_release(_payload(df_images=["acme-capability-svc"]))
    assert errors == []
    part = RF.partition_environments(details)
    # standard goes everywhere; prl1_only stops at uat/prl1; df excluded from all deploys
    assert part["prd"] == ["acme-workflow-service"]
    assert part["uat"] == ["acme-workflow-service", "acme-risk-fetcher"]
    assert part["prl1"] == part["uat"]
    assert part["dataflow_only"] == ["acme-capability-svc"]


def test_parse_release_payload_routes_to_release_type():
    req = _try_parse_json_payload(json.dumps(_payload()))
    assert req["deployment_type"] == "release"
    assert req["release"]["release_name"].startswith("July")
    assert {p["name"] for p in req["images"]} == {
        "acme-workflow-service", "acme-risk-fetcher", "acme-capability-svc"
    }


def test_release_files_marker_roundtrip():
    files = ["artefact-provider/artefact.json", ".github/workflows/deploy_with_sdlc_governance_uat.yaml"]
    body = f"Release X\n\n{RF._FILES_MARKER} {json.dumps(files)}\n"
    pr = SimpleNamespace(body=body)
    assert RF._release_files_from_pr(pr) == files
    assert RF._release_files_from_pr(SimpleNamespace(body="no marker")) == []


class _FakeContent(SimpleNamespace):
    pass


class _FakeGhRepo:
    """Branch->path->raw text; enough for promote_release."""

    def __init__(self, files):
        self.files = files  # {branch: {path: text}}
        self.prs = []
        self._n = 0

    def get_git_ref(self, name):
        return SimpleNamespace(object=SimpleNamespace(sha=name.split("heads/")[1]),
                               delete=lambda: None)

    def create_git_ref(self, ref, sha):
        self.files[ref.split("heads/")[1]] = dict(self.files.get(sha, {}))

    def get_contents(self, path, ref=None):
        if path not in self.files.get(ref, {}):
            raise Exception("404")
        return _FakeContent(decoded_content=self.files[ref][path].encode(), sha="sha")

    def update_file(self, path, msg, content, sha, branch):
        self.files[branch][path] = content

    def create_file(self, path, msg, content, branch):
        self.files[branch][path] = content

    def get_pulls(self, state="open", base=None, sort=None, direction=None):
        return [p for p in self.prs if p.state == state and (base is None or p.base.ref == base)]

    def create_pull(self, title, body, head, base):
        self._n += 1
        pr = SimpleNamespace(
            number=self._n, title=title, body=body, state="open",
            head=SimpleNamespace(ref=head), base=SimpleNamespace(ref=base),
            html_url=f"http://pr/{self._n}", mergeable=True, mergeable_state="clean",
            merge_commit_sha="m", update=lambda: None,
        )
        def merge(merge_method="squash", _pr=pr):
            self.files[_pr.base.ref].update(self.files.get(_pr.head.ref, {}))
            _pr.state = "closed"
        pr.merge = merge
        pr.edit = lambda **kw: None
        self.prs.append(pr)
        return pr


def test_promote_release_copies_fileset(monkeypatch):
    files = ["artefact-provider/artefact.json", ".github/workflows/deploy_with_sdlc_governance_uat.yaml"]
    repo = _FakeGhRepo({
        "release/rel-31": {files[0]: '{"artefact": ["u1"]}', files[1]: "name: uat\n"},
        "SIT": {files[0]: '{"artefact": ["u1"]}', files[1]: "name: uat\n"},
        "UAT": {files[0]: '{"artefact": []}', files[1]: "name: old\n"},
        "PRL1": {files[0]: '{"artefact": []}'},
    })
    # The release PR into SIT carries the marker.
    repo.create_pull(
        title="Release 31",
        body=f"{RF._FILES_MARKER} {json.dumps(files)}",
        head="release/rel-31", base="SIT",
    )
    monkeypatch.setattr(RF, "_get_github_client",
                        lambda: SimpleNamespace(get_repo=lambda full: repo))

    out = json.loads(RF.promote_release.invoke({"target": "uat"}))
    assert out["ok"] is True and out["action"] == "release_promoted"
    assert set(out["files"]) == set(files)
    assert repo.files["UAT"][files[0]] == '{"artefact": ["u1"]}'
    assert repo.files["UAT"][files[1]] == "name: uat\n"

    # Second promote: nothing left to change.
    out2 = json.loads(RF.promote_release.invoke({"target": "uat"}))
    assert out2["action"] == "no_change"
