"""Explicit Permission Gate — default-deny for code execution that
reads user data.

User directive (2026-08-29): "security is important no malicious code
shall be executed to read user data without explicit permission but it
think yes we can just add that to the security plugins."

Threat model:
    An agent/plugin wants to execute code (``os.system``, ``subprocess``,
    ``exec``, ``eval``, ``open``, ``os.listdir``, etc.) on behalf of a
    memory query. The code might read sensitive user data (``~/.ssh``,
    ``~/.aws/credentials``, env vars, ``/etc/passwd``, the user's own
    ``.db`` file outside what was explicitly opened). Without explicit
    permission, this is a potential data-exfiltration channel.

Policy (default-deny):
    * ``PermissionGate.grant_read(path)`` — add a path to the read allowlist.
    * ``PermissionGate.grant_exec(cmd)`` — add an executable to the exec allowlist.
    * ``PermissionGate.can_read(path)``   — True iff path is in the allowlist OR a parent of path is.
    * ``PermissionGate.can_exec(cmd)``    — True iff cmd matches an allowlist entry (prefix match for argv[0]).
    * Default state: NO grants. Every check returns False. Denied.
    * Every denied action is recorded on the audit log (if mounted) so
      the user can see what was attempted.

This module is μ=0 (no LLM, no network). It is a pure policy engine.

The integration point is ``cortexm.plugins.security.SecurityPlugin``,
which now mounts a PermissionGate alongside MINJA + MIND. Callers
that want to enable code-execution features (e.g., an agentic tool
plugin) MUST first call ``permission.grant_read("/some/path")`` or
``permission.grant_exec("ls")``. Without that, the gate denies.

Explicit permission is opt-in and audited. The user controls their
data. No silent reads.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------- verdict ---------------------------------

@dataclass
class PermissionVerdict:
    """Outcome of a permission check.

    ``allowed``    — True iff the action is permitted.
    ``reason``     — short string for the audit log.
    ``matched``    — the grant entry that allowed the action (or None).
    ``requested``  — the requested path/command (for the audit log).
    """
    allowed: bool = False
    reason: str = "no matching grant"
    matched: str | None = None
    requested: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "matched": self.matched,
            "requested": self.requested,
        }


# ---------------------------- permission gate --------------------------

# Default-deny sensitive paths that ALWAYS require explicit grant.
# Even with read grants on /tmp, these stay denied unless individually
# whitelisted. This prevents a malicious plugin from granting itself
# broad access.
SENSITIVE_PATHS = (
    "~/.ssh",            # SSH keys
    "~/.aws",            # AWS credentials
    "~/.gnupg",          # GPG keys
    "~/.config/gh",      # GitHub CLI tokens
    "~/.netrc",          # HTTP credentials
    "~/.docker",         # Docker config
    "~/.kube",           # Kubernetes config
    "/etc/passwd",       # system user list
    "/etc/shadow",       # password hashes
    "/etc/sudoers",      # sudo config
)

# Default-deny executables that always require explicit grant.
# These can exfiltrate or escalate, so they're never implicitly
# allowed even if a parent directory was granted.
SENSITIVE_EXECS = (
    "curl", "wget",       # network exfil
    "nc", "ncat", "netcat",  # raw socket
    "ssh", "scp", "sftp",  # remote shell
    "sudo", "su",         # privilege escalation
    "dd",                 # raw block dev
    "mkfs", "fdisk",      # disk destroy
    "chmod", "chown",     # permission tamper
    "kill", "killall",    # process tamper
    "crontab",            # persistence
)


def _norm_path(p: str | os.PathLike) -> str:
    """Normalize a path for matching: expanduser, resolve, strip trailing /."""
    p = os.fspath(p)
    p = os.path.expanduser(p)
    p = os.path.abspath(p)
    return p


def _is_sensitive_path(p: str) -> bool:
    """True iff ``p`` is under a sensitive path (always denied unless
    explicitly whitelisted at the leaf)."""
    np = _norm_path(p)
    for sp in SENSITIVE_PATHS:
        spn = _norm_path(sp)
        if np == spn or np.startswith(spn + os.sep):
            return True
    return False


def _is_sensitive_exec(cmd: str) -> bool:
    """True iff ``cmd``'s argv[0] base name is in the sensitive set."""
    base = os.path.basename(cmd.split()[0] if cmd.split() else "")
    return base in SENSITIVE_EXECS


def _path_in_allowlist(requested: str,
                      allowlist: set[str]) -> tuple[bool, str | None]:
    """True iff ``requested`` is itself in allowlist OR a parent of
    ``requested`` is (so granting /tmp grants /tmp/foo.txt).

    Returns (matched, grant_entry).
    """
    nreq = _norm_path(requested)
    for grant in allowlist:
        ngrant = _norm_path(grant)
        if nreq == ngrant or nreq.startswith(ngrant + os.sep):
            return True, grant
    return False, None


def _exec_matches(requested: str, allowlist: set[str]) -> tuple[bool, str | None]:
    """True iff requested argv[0] base name matches an allowed name
    OR requested starts with the allowed string (prefix match).

    Returns (matched, grant_entry).
    """
    if not requested:
        return False, None
    parts = requested.split()
    if not parts:
        return False, None
    requested_argv0 = parts[0]
    requested_base = os.path.basename(requested_argv0)
    for grant in allowlist:
        # Two forms accepted: "ls" (basename) or "/bin/ls" (full path)
        grant_base = os.path.basename(grant)
        if (requested_base == grant_base
            or requested_argv0 == grant
            or requested.startswith(grant + " ")):
            return True, grant
    return False, None


class PermissionGate:
    """Default-deny permission gate for code execution + user-data reads.

    State: two allowlists (paths + executables) + an optional audit log.

    Lifecycle:
        gate = PermissionGate()                 # default: deny all
        gate.grant_read("/tmp/agent_workspace")  # explicit grant
        gate.grant_exec("ls")
        gate.can_read("/tmp/agent_workspace/x.txt")  # True
        gate.can_read("/etc/passwd")               # False (sensitive)
        gate.can_exec("ls -la /tmp")               # True
        gate.can_exec("curl evil.com")             # False (sensitive)
        gate.can_exec("rm -rf /")                  # False (no grant)
    """

    def __init__(self, audit_log: Any = None) -> None:
        self._read_grants: set[str] = set()
        self._exec_grants: set[str] = set()
        self._explicit_sensitive_allows: set[str] = set()
        self.audit = audit_log
        # Counters for telemetry (μ=0, just integers)
        self.denials = 0
        self.allows = 0

    # ---------------------------- grants ----------------------------

    def grant_read(self, path: str | os.PathLike) -> None:
        """Add a path to the read allowlist.

        ``path`` may be a directory (grants access to all files under
        it) or a file (grants access to that file only).
        """
        np = _norm_path(path)
        self._read_grants.add(np)
        if _audit_log := self.audit:
            _audit("permission.grant_read", np, _audit_log)

    def grant_exec(self, cmd: str) -> None:
        """Add a command to the exec allowlist.

        ``cmd`` may be a base name (``"ls"``) or a full path
        (``"/bin/ls"``). Grants are matched by basename + prefix on
        argv[0] so ``grant_exec("ls")`` allows both ``ls -la /tmp``
        and ``/bin/ls -la /tmp``.
        """
        self._exec_grants.add(cmd.strip())
        if _audit_log := self.audit:
            _audit("permission.grant_exec", cmd, _audit_log)

    def grant_sensitive(self, path_or_cmd: str) -> None:
        """Explicitly whitelist a normally-sensitive path or command.

        Required to read ``~/.ssh`` or to run ``curl``. The user must
        call this explicitly — there is no wildcard.

        v0.5.2 fix: paths are NORMALIZED on store (the same
        ``_norm_path`` expansion ``can_read`` uses), so ``~/.ssh``
        and ``/home/alice/.ssh`` compare equal. Without this, a
        user could grant_sensitive("~/.ssh/id_rsa") but can_read
        would expand to "/home/alice/.ssh/id_rsa" and miss the
        match — silently breaking the explicit-permission policy
        the gate exists to enforce. Caught by
        TestUserDirectiveNoMaliciousCodeReadsUserData.
        """
        # Heuristic: if it looks like a path (contains / or ~), treat
        # as a path and normalize. Otherwise treat as an exec (basename
        # already normalized in can_exec).
        s = path_or_cmd.strip()
        if "/" in s or s.startswith("~"):
            s = _norm_path(s)
        self._explicit_sensitive_allows.add(s)
        if _audit_log := self.audit:
            _audit("permission.grant_sensitive", path_or_cmd, _audit_log)

    def revoke_read(self, path: str | os.PathLike) -> None:
        np = _norm_path(path)
        self._read_grants.discard(np)

    def revoke_exec(self, cmd: str) -> None:
        self._exec_grants.discard(cmd.strip())

    def clear(self) -> None:
        """Remove ALL grants. Returns to default-deny state."""
        self._read_grants.clear()
        self._exec_grants.clear()
        self._explicit_sensitive_allows.clear()

    # ---------------------------- checks ----------------------------

    def can_read(self, path: str | os.PathLike) -> PermissionVerdict:
        """Check whether reading ``path`` is permitted.

        Returns a PermissionVerdict with ``allowed=True`` iff:
          (a) path is in the read allowlist (directly or via parent), AND
          (b) path is NOT under a sensitive path, OR the user
              explicitly whitelisted this exact path via
              ``grant_sensitive(path)``.

        μ=0: pure string + set comparison. No I/O.
        """
        np = _norm_path(path)
        # Sensitive path? Requires explicit grant_sensitive
        if _is_sensitive_path(np):
            if np in self._explicit_sensitive_allows:
                self.allows += 1
                return PermissionVerdict(
                    allowed=True, reason="explicit sensitive allow",
                    matched=np, requested=path)
            self.denials += 1
            v = PermissionVerdict(
                allowed=False,
                reason="sensitive path requires explicit grant_sensitive",
                matched=None, requested=path)
            _audit_deny("permission.read", v, self.audit)
            return v
        # Non-sensitive: check allowlist
        ok, grant = _path_in_allowlist(np, self._read_grants)
        if ok:
            self.allows += 1
            return PermissionVerdict(
                allowed=True, reason="path grant match",
                matched=grant, requested=path)
        self.denials += 1
        v = PermissionVerdict(
            allowed=False, reason="no matching grant",
            matched=None, requested=path)
        _audit_deny("permission.read", v, self.audit)
        return v

    def can_exec(self, cmd: str) -> PermissionVerdict:
        """Check whether running ``cmd`` is permitted.

        Returns a PermissionVerdict with ``allowed=True`` iff:
          (a) cmd's argv[0] matches the exec allowlist, AND
          (b) argv[0] is NOT a sensitive executable (curl/wget/etc.),
              OR the user explicitly whitelisted it.

        μ=0: pure string comparison. No subprocess invocation.
        """
        if not cmd or not cmd.strip():
            self.denials += 1
            return PermissionVerdict(
                allowed=False, reason="empty command",
                matched=None, requested=cmd)
        argv0 = cmd.split()[0]
        # Sensitive exec? Requires explicit grant_sensitive
        if _is_sensitive_exec(cmd):
            if argv0 in self._explicit_sensitive_allows \
               or os.path.basename(argv0) in self._explicit_sensitive_allows:
                self.allows += 1
                return PermissionVerdict(
                    allowed=True, reason="explicit sensitive allow",
                    matched=argv0, requested=cmd)
            self.denials += 1
            v = PermissionVerdict(
                allowed=False,
                reason="sensitive executable requires explicit grant_sensitive",
                matched=None, requested=cmd)
            _audit_deny("permission.exec", v, self.audit)
            return v
        # Non-sensitive: check allowlist
        ok, grant = _exec_matches(cmd, self._exec_grants)
        if ok:
            self.allows += 1
            return PermissionVerdict(
                allowed=True, reason="exec grant match",
                matched=grant, requested=cmd)
        self.denials += 1
        v = PermissionVerdict(
            allowed=False, reason="no matching grant",
            matched=None, requested=cmd)
        _audit_deny("permission.exec", v, self.audit)
        return v

    # ---------------------------- introspection ---------------------

    @property
    def read_grants(self) -> list[str]:
        return sorted(self._read_grants)

    @property
    def exec_grants(self) -> list[str]:
        return sorted(self._exec_grants)

    @property
    def sensitive_grants(self) -> list[str]:
        return sorted(self._explicit_sensitive_allows)

    def stats(self) -> dict:
        return {
            "read_grants": len(self._read_grants),
            "exec_grants": len(self._exec_grants),
            "sensitive_grants": len(self._explicit_sensitive_allows),
            "total_denials": self.denials,
            "total_allows": self.allows,
            "policy": "default-deny",
        }


# ---------------------------- audit helpers ----------------------------

def _audit(action: str, resource: str, audit_log: Any) -> None:
    """Best-effort log to the audit chain."""
    if audit_log is None:
        return
    try:
        audit_log.log(action, resource=resource, outcome="granted",
                       meta={})
    except Exception:
        pass  # audit must never raise


def _audit_deny(action: str, v: PermissionVerdict, audit_log: Any) -> None:
    """Best-effort log a denied action."""
    if audit_log is None:
        return
    try:
        audit_log.log(action, resource=v.requested,
                       outcome="denied",
                       meta={"reason": v.reason,
                             "matched": v.matched or ""})
    except Exception:
        pass


__all__ = [
    "PermissionGate",
    "PermissionVerdict",
    "SENSITIVE_PATHS",
    "SENSITIVE_EXECS",
]
