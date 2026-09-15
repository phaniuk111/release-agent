"""The release flow without a git binary: Dulwich checks out, the REST API commits.

Runs against a REAL git repository on disk (Dulwich clones it exactly as it
would clone GitHub) and the deploy repo's updater script is a real script the
flow executes — only GitHub's API is faked.
"""
import os
from types import SimpleNamespace

import pytest
from dulwich import porcelain

from release_agent.tools import git_snapshot as G
from release_agent.tools import release_fileset as RF

TOKEN = "ghp_secretsecretsecret"

UPDATER = '''\
import argparse, json, os
import helper  # a sibling module: must not leave __pycache__ behind as a "change"
ap = argparse.ArgumentParser(); ap.add_argument("--release-details-file"); a = ap.parse_args()
d = json.load(open(a.release_details_file))
os.makedirs("artefact-provider", exist_ok=True)
with open("artefact-provider/artefact.json", "w") as f:
    json.dump({"artefact": d["artefact"]}, f, indent=2)
with open(".github/workflows/deploy_uat.yaml", "a") as f:
    f.write("# " + d["release_name"] + "\\n")
os.remove("obsolete.txt")
'''


def _commit_all(repo, files):
    porcelain.add(repo, [os.path.join(repo.path, p) for p in files])
    porcelain.commit(repo, message=b"base", author=b"a <a@x>", committer=b"a <a@x>")


@pytest.fixture
def remote(tmp_path):
    """A deploy repo with a SIT branch: workflows, a script, a file the release deletes."""
    path = tmp_path / "remote"
    repo = porcelain.init(str(path))
    files = {
        ".github/workflows/deploy_uat.yaml": "name: uat\n",
        "scripts/release/update_release_files.py": UPDATER,
        "scripts/release/helper.py": "X = 1\n",
        "scripts/run.sh": "#!/bin/sh\necho hi\n",
        "obsolete.txt": "old\n",
    }
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    os.chmod(path / "scripts/run.sh", 0o755)
    _commit_all(repo, list(files))
    porcelain.branch_create(repo, "sit")
    repo.close()
    return str(path)


# --- transport ------------------------------------------------------------------
def test_the_clusters_uppercase_proxy_reaches_dulwich(monkeypatch):
    """Dulwich itself reads only lowercase https_proxy; clusters set HTTPS_PROXY."""
    for k in ("https_proxy", "http_proxy", "all_proxy", "no_proxy", "NO_PROXY", "REQUESTS_CA_BUNDLE",
              "CURL_CA_BUNDLE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp-ca.pem")
    cfg = G.transport_config("https://github.com/o/r.git")
    assert cfg.get((b"http",), b"proxy") == b"http://proxy.corp.example:8080"
    assert cfg.get((b"http",), b"sslCAInfo") == b"/etc/ssl/corp-ca.pem", "same CA requests trusts"


def test_no_proxy_is_honoured(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:8080")
    monkeypatch.setenv("NO_PROXY", "github.example")
    cfg = G.transport_config("https://github.example/o/r.git")
    with pytest.raises(KeyError):
        cfg.get((b"http",), b"proxy")


def test_the_clone_url_never_carries_the_token():
    assert G.repo_url("o/r") == "https://github.com/o/r.git"


def test_a_clone_failure_never_shows_the_token(monkeypatch):
    def boom(*a, **kw):
        raise OSError(f"401 for https://x-access-token:{TOKEN}@github.com/o/r.git")

    monkeypatch.setattr("dulwich.porcelain.clone", boom)
    with pytest.raises(G.SnapshotError) as e:
        G.clone_branch("https://github.com/o/r.git", "sit", "/tmp/nowhere", TOKEN)
    assert TOKEN not in str(e.value) and "could not fetch sit over github.com" in str(e.value)


# --- checkout + staging -------------------------------------------------------
def test_checkout_is_one_commit_of_the_branch(remote, tmp_path):
    dest = str(tmp_path / "co")
    head = G.clone_branch(remote, "sit", dest, "")
    assert len(head) == 40 and G.base_commit(dest) == head
    assert os.path.isfile(os.path.join(dest, "obsolete.txt"))


def test_staging_matches_git_add_all(remote, tmp_path):
    dest = str(tmp_path / "co")
    G.clone_branch(remote, "sit", dest, "")
    (tmp_path / "co/.gitignore").write_text("*.log\n")
    (tmp_path / "co/new/deep").mkdir(parents=True)
    (tmp_path / "co/new/deep/n.yaml").write_text("n: 1\n")
    (tmp_path / "co/obsolete.txt").unlink()
    (tmp_path / "co/.github/workflows/deploy_uat.yaml").write_text("name: changed\n")
    (tmp_path / "co/run.log").write_text("ignored\n")
    staged = G.stage_all(dest)
    assert staged == {"add": [".gitignore", "new/deep/n.yaml"],
                      "modify": [".github/workflows/deploy_uat.yaml"],
                      "delete": ["obsolete.txt"]}


# --- commit through the API ----------------------------------------------------
class _FakeRepo:
    """GitHub's Git Data API, recorded."""

    def __init__(self):
        self.blobs, self.trees, self.commits, self.refs, self.pulls = [], [], [], [], []

    def create_git_blob(self, content, encoding):
        self.blobs.append((content, encoding))
        return SimpleNamespace(sha=f"blob{len(self.blobs)}")

    def get_git_commit(self, sha):
        return SimpleNamespace(sha=sha, tree=f"tree-of-{sha}")

    def create_git_tree(self, elements, base_tree=None):
        self.trees.append(([e._identity for e in elements], base_tree))
        return "new-tree"

    def create_git_commit(self, message, tree, parents, author=None):
        self.commits.append((message, tree, [p.sha for p in parents], author._identity))
        return SimpleNamespace(sha="c0ffee")

    def create_git_ref(self, ref, sha):
        self.refs.append((ref, sha))

    def create_pull(self, **kw):
        self.pulls.append(kw)
        return SimpleNamespace(number=12, html_url="https://github.example/o/deploy/pull/12")


def test_commit_holds_exactly_the_staged_changes_with_their_modes(remote, tmp_path):
    import base64

    dest = str(tmp_path / "co")
    base = G.clone_branch(remote, "sit", dest, "")
    (tmp_path / "co/scripts/run.sh").write_text("#!/bin/sh\necho changed\n")
    (tmp_path / "co/obsolete.txt").unlink()
    staged = G.stage_all(dest)
    gh = _FakeRepo()
    assert G.commit_via_api(gh, dest, staged, base, "R1", "dev", "dev@example.com") == "c0ffee"

    elements, base_tree = gh.trees[0]
    assert base_tree == f"tree-of-{base}", "built on the commit that was checked out"
    by_path = {e["path"]: e for e in elements}
    assert by_path["scripts/run.sh"]["mode"] == "100755", "executable bit survives"
    assert by_path["obsolete.txt"]["sha"] is None, "a deletion is a null entry"
    assert base64.b64decode(gh.blobs[0][0]) == b"#!/bin/sh\necho changed\n"
    assert gh.commits[0][2] == [base]
    assert gh.commits[0][3]["email"] == "dev@example.com"


def test_nothing_staged_commits_nothing(remote, tmp_path):
    dest = str(tmp_path / "co")
    base = G.clone_branch(remote, "sit", dest, "")
    with pytest.raises(G.SnapshotError):
        G.commit_via_api(_FakeRepo(), dest, {"add": [], "modify": [], "delete": []}, base, "R", "d", "d@x")


# --- the whole release: prepare (preview) then apply on a fresh checkout -------
PAYLOAD = {
    "release_name": "Release 42",
    "start_date": "2026-09-20 18:00:00",
    "end_date": "2026-09-20 20:00:00",
    "change_initiator": "dev@example.com",
    "change_summary": "weekly",
    "artefact": ["payments-api:1.2.0"],
    "deployment_repo": "o/deploy",
}


@pytest.fixture
def wired(monkeypatch, remote):
    gh_repo = _FakeRepo()
    monkeypatch.setattr(G, "repo_url", lambda repo_full: remote)
    monkeypatch.setattr(RF, "_resolve_github_token", lambda: "")
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda name: gh_repo))
    monkeypatch.setattr(RF, "_open_prd_pr_blocker", lambda repo: None)
    monkeypatch.setattr(RF, "_merge_pr", lambda pr, method: (True, "merged"))
    monkeypatch.setattr(RF.settings, "sit_branch", "sit", raising=False)
    monkeypatch.setattr(RF.settings, "artifactory_base_url", "", raising=False)
    return gh_repo


def test_preview_then_apply_without_a_git_binary(wired, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")          # no git, anywhere
    prep = RF.prepare_release_fileset(dict(PAYLOAD))
    assert prep["ok"], prep
    assert prep["preview"]["changed_files"] == [
        ".github/workflows/deploy_uat.yaml", "artefact-provider/artefact.json", "obsolete.txt",
    ], "no __pycache__ from the updater's own imports"
    assert "workdir" not in prep, "preview leaves no checkout behind"

    out = RF.apply_release_fileset(prep)
    assert out["ok"], out
    assert wired.refs == [("refs/heads/release/release-42", "c0ffee")]
    paths = sorted(e["path"] for e in wired.trees[0][0])
    assert paths == prep["preview"]["changed_files"]
    assert wired.pulls[0]["head"] == "release/release-42" and wired.pulls[0]["base"] == "sit"
    assert out["merged_to_sit"] is True


def test_an_existing_branch_is_refused_in_plain_words(wired):
    def taken(ref, sha):
        raise Exception("Reference already exists")

    wired.create_git_ref = taken
    out = RF.apply_release_fileset(RF.prepare_release_fileset(dict(PAYLOAD)))
    assert not out["ok"] and "already exists" in out["error"] and not wired.pulls
