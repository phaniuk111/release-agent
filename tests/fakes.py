"""Shared fake PyGithub repo/PR for tests around the branch+deployment.json model.

Every deploy/release/promotion tool works against the same shape: a repo with
``files[branch][path]`` (raw text — a JSON deployment file, a governed-deploy
workflow YAML, an artefact.json, ...) and pull requests with ``.head``/``.base``
ref objects, a ``mergeable``/``mergeable_state`` GitHub computes async, and a
``merge()`` that copies the head's files onto the base and closes the PR.

Reconciled from five copy-pasted fakes that had already drifted — in particular
one stored ``pr.head`` as ``SimpleNamespace(ref=...)`` while another stored the
same thing as two plain strings (``pr.head_b``/``pr.base_b``). This is the one
shape every caller now gets.

Kept small and subclassable rather than trying to grow into every fake GitHub
behaviour: tests/test_concurrency.py's ``_RacingRepo`` (a race hook fired from
``create_pull``) and its own ``_ProtectedRepo`` (a 405 branch-protection
refusal) are genuinely different behaviour, not scaffolding, and are not
folded in here — a repo needing that kind of hook can subclass ``FakeRepo`` and
override ``create_pull``/``FakePR.merge`` the way tests/test_change_request.py's
``_ProtectedRepo`` does.
"""
import hashlib
import json
from types import SimpleNamespace


def blob_sha(text):
    """Git's blob id for ``text`` — content-addressed, like GitHub's."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeContent:
    """What ``repo.get_contents(...)`` returns: decoded_content + sha."""

    def __init__(self, text, sha="sha"):
        self.decoded_content = text.encode()
        self.sha = sha


class FakeRef:
    """What ``repo.get_git_ref(...)`` returns: an object with the branch tip's
    sha, and a delete() for cleaning up a short-lived change branch (it drops
    the branch from ``files`` when the ref came from a FakeRepo)."""

    def __init__(self, sha, repo=None):
        self.object = SimpleNamespace(sha=sha)
        self._repo = repo

    def delete(self):
        if self._repo is not None:
            self._repo.files.pop(self.object.sha, None)


class FakePR:
    """A PyGithub PullRequest stand-in.

    ``head``/``base`` are ref objects (``.ref``), matching PyGithub — not the
    plain strings one of the drifted copies used. ``merge()`` copies the head
    branch's files onto the base branch and closes the PR; ``edit(state=...)``
    can close/reopen without merging (used to retire a stale staging PR).
    """

    def __init__(self, repo, head, base, title="", body=""):
        self.repo = repo
        self.head = SimpleNamespace(ref=head)
        self.base = SimpleNamespace(ref=base)
        self.title, self.body = title, body
        self.state = "open"
        repo._pr += 1
        self.number = repo._pr
        self.html_url = f"http://pr/{self.number}"
        self.mergeable, self.mergeable_state, self.merge_commit_sha = True, "clean", "msha"

    def update(self):
        pass

    def edit(self, state=None, **kwargs):
        if state:
            self.state = state

    def merge(self, merge_method="squash", commit_message=None):
        self.merge_message = commit_message
        self.repo.files.setdefault(self.base.ref, {}).update(
            self.repo.files.get(self.head.ref, {})
        )
        self.state = "closed"


class FakeRepo:
    """Minimal PyGithub Repository stand-in: ``files[branch][path]`` = raw text.

    ``initial`` values may be a dict (auto ``json.dumps``'d — the
    deployment.json callers' shape) or already a string (release_fileset's
    file-set is a mix of JSON and YAML text) — either way ``files[branch][path]``
    ends up as the raw text ``get_contents``/``create_file``/``update_file`` work
    with.

    A commit sha is the branch's NAME (``get_git_ref``, ``get_branch``), and a
    blob sha is git's content hash of the file, so a file that changed has a
    new sha. ``strict_sha = True`` makes ``update_file`` refuse a stale blob sha
    the way GitHub does (409); off by default, so older callers keep passing
    whatever sha they like.
    """

    strict_sha = False

    def __init__(self, initial):
        self.files = {
            b: {p: d if isinstance(d, str) else json.dumps(d) for p, d in fs.items()}
            for b, fs in initial.items()
        }
        self.prs = []
        self.writes = []          # every create_file/update_file: path, msg, branch, author, sha
        self._pr = 0

    def get_git_ref(self, name):
        return FakeRef(name.split("heads/", 1)[1], repo=self)  # sha == branch name

    def get_branch(self, name):
        if name not in self.files:
            raise Exception("404 Branch not found")
        return SimpleNamespace(name=name, commit=SimpleNamespace(sha=name))

    def create_git_ref(self, ref, sha):
        work = ref.split("heads/", 1)[1]
        if work in self.files:
            raise Exception('422 {"message": "Reference already exists"}')
        self.files[work] = dict(self.files.get(sha, {}))

    def get_contents(self, path, ref=None):
        fs = self.files.get(ref, {})
        if path not in fs:
            raise Exception("404")
        return FakeContent(fs[path], sha=blob_sha(fs[path]))

    def create_file(self, path, msg, content, branch=None, author=None):
        self.writes.append({"path": path, "msg": msg, "branch": branch, "author": author})
        self.files.setdefault(branch, {})[path] = content

    def update_file(self, path, msg, content, sha, branch=None, author=None):
        current = self.files.get(branch, {}).get(path)
        if self.strict_sha and (current is None or blob_sha(current) != sha):
            raise Exception(f"409 {path} does not match {sha}")
        self.writes.append({"path": path, "msg": msg, "branch": branch, "author": author, "sha": sha})
        self.files.setdefault(branch, {})[path] = content

    def create_pull(self, title, body, head, base):
        pr = FakePR(self, head, base, title=title, body=body)
        self.prs.append(pr)
        return pr

    def get_pulls(self, state="open", base=None, sort=None, direction=None, head=None):
        """``head`` is GitHub's "owner:branch" filter."""
        branch = head.split(":", 1)[-1] if head else None
        return [p for p in self.prs if p.state == state and (base is None or p.base.ref == base)
                and (branch is None or p.head.ref == branch)]
