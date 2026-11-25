#!/usr/bin/env python3
"""Security gate: turns raw scanner output into a pass/fail decision (POLICY.md §2, §3).

Inputs (in --results): gitleaks.json, semgrep.json, bandit.json, osv.json, trivy.json, status.json.
Repo files (in --repo):  .zibloh-ci/baseline.json, .zibloh-ci/exceptions.yml.

Fails (exit 1) when:
  - a scanner errored or its output is missing/unreadable (fail closed);
  - a blocking finding is neither in the baseline nor covered by a valid exception;
  - an exception is malformed or expired.

--write-baseline PATH writes every current blocking finding not covered by an exception to PATH
(used once when a repo adopts the gate, then worked down).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

BLOCKING_SEMGREP = {"ERROR", "CRITICAL", "HIGH"}
BLOCKING_TRIVY = {"CRITICAL", "HIGH"}
OSV_BLOCK_SCORE = 7.0          # CVSS >= 7.0 (HIGH/CRITICAL) with a fixed version available
MAX_EXCEPTION_DAYS = 184       # ~6 months
TOOLS = ("gitleaks", "semgrep", "bandit", "osv", "trivy")


@dataclass
class Finding:
    tool: str
    rule: str
    path: str
    line: int
    severity: str
    message: str
    blocking: bool
    fingerprint: str = ""
    status: str = "new"        # new | baseline | excepted | info
    extra: dict = field(default_factory=dict)

    @property
    def rule_id(self) -> str:
        return f"{self.tool}:{self.rule}"


def _h(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


class SourceLines:
    """Reads source lines so code fingerprints survive line shifts."""

    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, list[str]] = {}

    def get(self, path: str, line: int) -> str:
        if path not in self.cache:
            p = Path(path)
            p = p if p.is_absolute() else self.root / p
            try:
                self.cache[path] = p.read_text(errors="replace").splitlines()
            except OSError:
                self.cache[path] = []
        lines = self.cache[path]
        return " ".join(lines[line - 1].split()) if 0 < line <= len(lines) else ""


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)
    return str(p).removeprefix("./")


def _load_json(path: Path):
    with path.open() as f:
        text = f.read()
    return json.loads(text) if text.strip() else None


# ---------------------------------------------------------------- parsers
def parse_gitleaks(data, root: Path, src: SourceLines) -> list[Finding]:
    out = []
    for r in data or []:
        path = _rel(r.get("File", ""), root)
        out.append(Finding(
            tool="gitleaks", rule=r.get("RuleID", "unknown"), path=path,
            line=int(r.get("StartLine") or 0), severity="SECRET",
            message=r.get("Description", "secret"), blocking=True,
            fingerprint=_h("gitleaks", r.get("Fingerprint") or f"{path}:{r.get('RuleID')}:{r.get('StartLine')}"),
            extra={"commit": r.get("Commit", ""), "author": r.get("Author", "")},
        ))
    return out


def parse_semgrep(data, root: Path, src: SourceLines) -> list[Finding]:
    out = []
    for r in (data or {}).get("results", []):
        path = _rel(r.get("path", ""), root)
        line = int(r.get("start", {}).get("line") or 0)
        sev = str(r.get("extra", {}).get("severity", "INFO")).upper()
        rule = r.get("check_id", "unknown")
        # Local rules get the config directory baked into their id ("home.runner...rules.semgrep.zibloh-x"):
        # keep only the rule id so fingerprints match across machines.
        # The path itself can contain "zibloh-" (".../Milmoh/zibloh-ci/v1/rules/..."): cut at the last one.
        if ".zibloh-" in rule:
            rule = rule[rule.rindex(".zibloh-") + 1:]
        out.append(Finding(
            tool="semgrep", rule=rule, path=path, line=line, severity=sev,
            message=(r.get("extra", {}).get("message") or "").strip().splitlines()[0][:300] if r.get("extra", {}).get("message") else "",
            blocking=sev in BLOCKING_SEMGREP,
            fingerprint=_h("semgrep", rule, path, src.get(path, line)),
            extra={"code": src.get(path, line)},
        ))
    return out


def parse_bandit(data, root: Path, src: SourceLines) -> list[Finding]:
    out = []
    for r in (data or {}).get("results", []):
        path = _rel(r.get("filename", ""), root)
        line = int(r.get("line_number") or 0)
        sev = str(r.get("issue_severity", "LOW")).upper()
        conf = str(r.get("issue_confidence", "LOW")).upper()
        rule = f"{r.get('test_id', '')}-{r.get('test_name', '')}"
        out.append(Finding(
            tool="bandit", rule=rule, path=path, line=line, severity=f"{sev}/{conf}",
            message=r.get("issue_text", ""),
            blocking=sev == "HIGH" and conf in ("HIGH", "MEDIUM"),
            fingerprint=_h("bandit", rule, path, src.get(path, line)),
            extra={"code": src.get(path, line)},
        ))
    return out


def _osv_fixed(vulns: list[dict], ids: set[str], pkg_name: str) -> list[str]:
    fixed = []
    for v in vulns:
        if v.get("id") not in ids:
            continue
        for aff in v.get("affected", []):
            if str(aff.get("package", {}).get("name", "")).lower() != pkg_name.lower():
                continue
            for rng in aff.get("ranges", []):
                if rng.get("type") == "GIT":   # commit hashes, not installable versions
                    continue
                fixed += [e["fixed"] for e in rng.get("events", []) if "fixed" in e]
    return sorted(set(fixed))


def parse_osv(data, root: Path, src: SourceLines) -> list[Finding]:
    out = []
    for res in (data or {}).get("results", []) or []:
        source = _rel(res.get("source", {}).get("path", ""), root)
        for p in res.get("packages", []) or []:
            pkg = p.get("package", {})
            name, version, eco = pkg.get("name", ""), pkg.get("version", ""), pkg.get("ecosystem", "")
            vulns = p.get("vulnerabilities", []) or []
            groups = p.get("groups") or [{"ids": [v.get("id")], "max_severity": ""} for v in vulns]
            for g in groups:
                ids = set(g.get("ids", []))
                primary = sorted(ids)[0] if ids else "unknown"
                try:
                    score = float(g.get("max_severity") or "nan")
                except ValueError:
                    score = float("nan")
                fixed = _osv_fixed(vulns, ids, name)
                sev = "UNKNOWN" if score != score else ("CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW")
                aliases = sorted(set(g.get("aliases", [])) - {primary})
                out.append(Finding(
                    tool="osv", rule=primary, path=source, line=0, severity=sev,
                    message=f"{eco} {name} {version}" + (f" (fixed in {', '.join(fixed)})" if fixed else " (no fix yet)")
                            + (f" aka {', '.join(aliases[:3])}" if aliases else ""),
                    blocking=(score == score and score >= OSV_BLOCK_SCORE and bool(fixed)),
                    fingerprint=_h("osv", primary, eco, name, version, source),
                    extra={"package": name, "version": version, "fixed": fixed, "score": g.get("max_severity", "")},
                ))
    return out


def parse_trivy(data, root: Path, src: SourceLines) -> list[Finding]:
    out = []
    for res in (data or {}).get("Results", []) or []:
        path = _rel(res.get("Target", ""), root)
        for m in res.get("Misconfigurations", []) or []:
            if m.get("Status", "FAIL") != "FAIL":
                continue
            sev = str(m.get("Severity", "UNKNOWN")).upper()
            cause = m.get("CauseMetadata", {}) or {}
            rule = m.get("ID") or m.get("AVDID") or "unknown"
            out.append(Finding(
                tool="trivy", rule=rule, path=path, line=int(cause.get("StartLine") or 0),
                severity=sev, message=m.get("Title", ""),
                blocking=sev in BLOCKING_TRIVY,
                fingerprint=_h("trivy", rule, path, str(cause.get("Resource", ""))),
            ))
    return out


PARSERS = {
    "gitleaks": ("gitleaks.json", parse_gitleaks),
    "semgrep": ("semgrep.json", parse_semgrep),
    "bandit": ("bandit.json", parse_bandit),
    "osv": ("osv.json", parse_osv),
    "trivy": ("trivy.json", parse_trivy),
}


# ---------------------------------------------------------------- exceptions / baseline
@dataclass
class ExceptionRule:
    id: str
    path: str
    reason: str
    owner: str
    expires: dt.date
    match: str = ""            # optional: substring of the flagged source line (pins the exception to that code)
    used: int = 0

    def matches(self, f: Finding) -> bool:
        if self.id not in (f.rule_id, f"{f.tool}:*", f.fingerprint):
            return False
        if self.path and not fnmatch.fnmatch(f.path, self.path):
            return False
        return not self.match or self.match in f.extra.get("code", "")


def load_exceptions(repo: Path, today: dt.date) -> tuple[list[ExceptionRule], list[str]]:
    path = repo / ".zibloh-ci" / "exceptions.yml"
    if not path.exists():
        return [], []
    import yaml  # installed with the scanners

    errors, rules = [], []
    raw = yaml.safe_load(path.read_text()) or []
    if not isinstance(raw, list):
        return [], [f"{path}: must be a YAML list"]
    for i, e in enumerate(raw, 1):
        where = f"exceptions.yml entry {i}"
        if not isinstance(e, dict):
            errors.append(f"{where}: not a mapping")
            continue
        missing = [k for k in ("id", "reason", "owner", "expires") if not e.get(k)]
        if missing:
            errors.append(f"{where}: missing {', '.join(missing)}")
            continue
        exp = e["expires"]
        if isinstance(exp, str):
            try:
                exp = dt.date.fromisoformat(exp)
            except ValueError:
                errors.append(f"{where}: expires must be YYYY-MM-DD")
                continue
        if not isinstance(exp, dt.date):
            errors.append(f"{where}: expires must be YYYY-MM-DD")
            continue
        if exp < today:
            errors.append(f"{where} ({e['id']}): expired on {exp}")
            continue
        if (exp - today).days > MAX_EXCEPTION_DAYS:
            errors.append(f"{where} ({e['id']}): expiry more than 6 months away ({exp})")
            continue
        rules.append(ExceptionRule(id=str(e["id"]), path=str(e.get("path") or ""),
                                   reason=str(e["reason"]), owner=str(e["owner"]), expires=exp,
                                   match=" ".join(str(e.get("match") or "").split())))
    return rules, errors


def load_baseline(repo: Path) -> dict:
    path = repo / ".zibloh-ci" / "baseline.json"
    if not path.exists():
        return {}
    return (json.loads(path.read_text()) or {}).get("fingerprints", {})


# ---------------------------------------------------------------- main
def evaluate(results: Path, repo: Path, today: dt.date):
    errors: list[str] = []
    status = {}
    try:
        status = _load_json(results / "status.json") or {}
    except (OSError, ValueError) as exc:
        errors.append(f"status.json unreadable: {exc}")
    tool_status = status.get("tools", {})

    src = SourceLines(Path(status.get("target") or repo))
    root = Path(status.get("target") or repo)
    findings: list[Finding] = []
    for tool, (fname, parser) in PARSERS.items():
        st = tool_status.get(tool, "missing")
        if st.startswith("skipped"):
            continue
        if st != "ok":
            errors.append(f"{tool}: {st}")
            continue
        try:
            findings += parser(_load_json(results / fname), root, src)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{tool}: cannot read {fname}: {exc}")

    exceptions, exc_errors = load_exceptions(repo, today)
    errors += exc_errors
    baseline = load_baseline(repo)

    for f in findings:
        if not f.blocking:
            f.status = "info"
            continue
        rule = next((e for e in exceptions if e.matches(f)), None)
        if rule:
            rule.used += 1
            f.status = "excepted"
        elif f.fingerprint in baseline:
            f.status = "baseline"

    seen = {f.fingerprint for f in findings}
    stale_baseline = [fp for fp in baseline if fp not in seen]
    unused_exceptions = [e for e in exceptions if e.used == 0]
    return findings, errors, exceptions, stale_baseline, unused_exceptions, status


def summary_md(findings, errors, stale, unused, status) -> str:
    new = [f for f in findings if f.status == "new"]
    count = lambda s: sum(1 for f in findings if f.status == s)
    ok = not new and not errors
    lines = [f"## Security gate: {'✅ passed' if ok else '❌ failed'}", ""]
    lines.append(f"New blocking: **{len(new)}** · baseline: {count('baseline')} · excepted: {count('excepted')} · "
                 f"info: {count('info')} · gitleaks mode: `{status.get('gitleaks_mode', '?')}`")
    lines.append("")
    if errors:
        lines += ["### Errors", ""] + [f"- {e}" for e in errors] + [""]
    if new:
        lines += ["### New blocking findings", "", "| Tool | Rule | Location | Severity | Detail |", "|---|---|---|---|---|"]
        for f in sorted(new, key=lambda x: (x.tool, x.path, x.line)):
            loc = f"`{f.path}:{f.line}`" if f.line else f"`{f.path}`"
            lines.append(f"| {f.tool} | `{f.rule}` | {loc} | {f.severity} | {f.message.replace('|', '/')[:160]} |")
        lines += ["", "Fix them, or add a justified exception to `.zibloh-ci/exceptions.yml` (see zibloh-ci POLICY.md §3).", ""]
    if stale:
        lines += [f"ℹ️ {len(stale)} baseline entr{'y is' if len(stale) == 1 else 'ies are'} no longer found: prune `.zibloh-ci/baseline.json`.", ""]
    if unused:
        lines += [f"ℹ️ Unused exceptions: {', '.join(e.id for e in unused)}", ""]
    tools = status.get("tools", {})
    vers = status.get("versions", {})
    lines += ["<details><summary>Scanners</summary>", ""]
    lines += [f"- {t}: {tools.get(t, 'missing')} ({vers.get('osv-scanner' if t == 'osv' else t, '')})" for t in TOOLS]
    lines += ["", "</details>"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--report", type=Path, help="write the full JSON report here")
    ap.add_argument("--write-baseline", type=Path)
    ap.add_argument("--today", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    findings, errors, exceptions, stale, unused, status = evaluate(args.results, args.repo, today)

    if args.write_baseline:
        fps = {f.fingerprint: {"tool": f.tool, "rule": f.rule, "path": f.path, "line": f.line, "severity": f.severity}
               for f in findings if f.status in ("new", "baseline")}
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(json.dumps(
            {"version": 1, "created": today.isoformat(), "fingerprints": dict(sorted(fps.items()))}, indent=2) + "\n")
        print(f"baseline written: {len(fps)} findings -> {args.write_baseline}")

    md = summary_md(findings, errors, stale, unused, status)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md + "\n")
    if args.report:
        args.report.write_text(json.dumps({
            "date": today.isoformat(),
            "passed": not errors and not any(f.status == "new" for f in findings),
            "errors": errors,
            "status": status,
            "findings": [asdict(f) for f in findings],
            "exceptions": [{**asdict(e), "expires": e.expires.isoformat()} for e in exceptions],
            "stale_baseline": stale,
        }, indent=2) + "\n")

    if args.write_baseline:
        return 1 if errors else 0
    return 1 if errors or any(f.status == "new" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
