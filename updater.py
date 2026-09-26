# SPDX-License-Identifier: MIT
"""Self-update app.py from a GitHub repo, with automatic rollback.

The timer calls check() on each brew. Only app.py is ever replaced. This file, code.py (the loader) and boot.py
are installed by hand and never auto-updated, so a bad push can't break
the machinery that recovers from it.

settings.toml:
  GITHUB_REPO   = "numeric-io/coffee-timer"
  GITHUB_TOKEN  = "github_pat_..."   (fine-grained, Contents: read-only;
                                      omit for a public repo)
  GITHUB_BRANCH = "main"             (optional)

Install: app.py.new is written and validated, the running app.py becomes
app.py.bak, and the new file takes its place. The next TRIAL_BOOTS boots
are a trial: if the new app crashes (at import or in main) during the
trial, the loader restores app.py.bak and reloads. After the trial the
update is kept. State lives in update.json, which needs CIRCUITPY to be
writable by code (see boot.py); when it isn't, updates are skipped.
"""

import os
import time

ROOT = "/"
APP = ROOT + "app.py"
NEW = ROOT + "app.py.new"
BAK = ROOT + "app.py.bak"
STATE = ROOT + "update.json"

TRIAL_BOOTS = 3
APP_MARKER = "def main("  # a real app.py always defines main()


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def load():
    try:
        import json
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save(state):
    """Write update.json. Returns False when CIRCUITPY is read-only to
    code (computer-edit mode, or no boot.py)."""
    try:
        import json
        with open(STATE, "w") as f:
            json.dump(state, f)
        return True
    except OSError:
        return False


def configured():
    return bool(os.getenv("GITHUB_REPO"))


# --------------------------------------------------------------- trial


def begin_boot():
    """Called by the loader before importing app.py: counts trial boots
    and keeps the update once the trial passes."""
    state = load()
    if "trial" not in state:
        return
    state["trial"] += 1
    if state["trial"] > TRIAL_BOOTS:
        del state["trial"]  # app.py.bak stays as a manual fallback
        print("update: %s kept after %d clean boots"
              % (state.get("sha", "?")[:7], TRIAL_BOOTS))
    save(state)


def in_trial():
    return "trial" in load()


def rollback(reason):
    """Restore app.py.bak if a fresh update is failing. Returns True if it
    rolled back (the caller should then reload)."""
    state = load()
    if "trial" not in state or not _exists(BAK):
        return False
    try:
        _remove(APP)
        os.rename(BAK, APP)
    except OSError as e:
        print("update: rollback failed: %r" % (e,))
        return False
    bad = state.get("sha", "")
    state["sha"] = state.get("prev_sha", "")
    state["bad_sha"] = bad  # don't reinstall the same broken commit
    del state["trial"]
    save(state)
    print("update: rolled back %s (%s)" % (bad[:7], reason))
    return True


# --------------------------------------------------------------- GitHub


def _headers(accept):
    h = {"Accept": accept, "User-Agent": "coffee-timer",
         "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _get(session, url, accept, timeout):
    r = session.get(url, headers=_headers(accept), timeout=timeout)
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d from %s: %s"
                          % (r.status_code, url.split("/repos/")[-1],
                             r.text[:80]))
        return r.text
    finally:
        r.close()


def _valid(source):
    if APP_MARKER not in source:
        return "missing %r" % APP_MARKER
    try:
        compile(source, "app.py", "exec")  # catches syntax errors
    except NameError:
        pass  # this build has no compile(); rely on the marker + trial
    except Exception as e:  # SyntaxError, MemoryError, ...
        return repr(e)
    return None


def check(join, budget=30):
    """Install a new app.py from GitHub if the branch moved. `join(deadline)`
    connects Wi-Fi. Best effort: returns "updated", "current", or a short
    reason it didn't update. Never raises."""
    repo = os.getenv("GITHUB_REPO")
    if not repo:
        return "not configured"
    state = load()
    state["last_check"] = time.time()  # for debugging; also tests writability
    if not save(state):
        return "read-only (computer-edit mode?)"
    deadline = time.monotonic() + budget
    try:
        import wifi
        import socketpool
        import ssl
        import adafruit_requests
        if not join(deadline):
            return "no Wi-Fi"
        branch = os.getenv("GITHUB_BRANCH") or "main"
        session = adafruit_requests.Session(socketpool.SocketPool(wifi.radio),
                                            ssl.create_default_context())
        base = "https://api.github.com/repos/" + repo
        sha = _get(session, base + "/commits/" + branch,
                   "application/vnd.github.sha", 10).strip()
        if sha == state.get("sha") or sha == state.get("bad_sha"):
            save(state)
            return "current"
        source = _get(session, base + "/contents/app.py?ref=" + sha,
                      "application/vnd.github.raw",
                      max(5, deadline - time.monotonic()))
        try:
            with open(APP) as f:
                if f.read() == source:  # e.g. the first check after install
                    state["sha"] = sha
                    save(state)
                    return "current"
        except OSError:
            pass
        problem = _valid(source)
        if problem:
            state["bad_sha"] = sha
            save(state)
            return "rejected %s: %s" % (sha[:7], problem)
        with open(NEW, "w") as f:
            f.write(source)
        _remove(BAK)
        os.rename(APP, BAK)
        try:
            os.rename(NEW, APP)
        except OSError:
            os.rename(BAK, APP)  # never leave the board without an app.py
            raise
        state["prev_sha"] = state.get("sha", "")
        state["sha"] = sha
        state["trial"] = 0
        state.pop("bad_sha", None)
        save(state)
        return "updated to " + sha[:7]
    except Exception as e:
        save(state)
        return "failed: %r" % (e,)
