"""Maintainer tool: rebuild the casey-ops demo offline from fixtures.

Purpose
    demo/casey-ops is a full, readable run for a fictional job seeker, so a visitor can judge
    the tool on GitHub without installing it. Every generated file in it comes from
    the real commands, so the demo can never describe behavior the code does not have, and
    tests/test_demo.py fails when a committed file differs from a fresh build.

Logic
    1. configure_offline points the board directory at the three clean fixture boards, replaces
       the network transport with one that serves tests/fixtures/pipeline/http by URL, freezes
       the clock at 2026-11-17 12:00 UTC and zeroes the pacing. No socket is opened.
    2. build_workspace runs the start skill's command order through cli.main: init,
       profile-import, criteria-build, sample, titles-check, titles-apply,
       scan, criteria-test, calibrate, function-review, rank, then the letter skill's
       letter-start and letter-check on Casey's dictated answers and cut draft (inputs/letter-*).
       Three steps are agent or user written in a real run (the title proposal, the user's decisions, the function-review
       verdicts); here they are demo inputs read from demo/casey-ops/inputs, or written by the
       builder as a keep verdict for every listed role. Cutoffs are the ones criteria-test
       proposes, accepted as Casey would accept them.
    3. render_demo copies GENERATED_FILES from the workspace to the demo folder byte for byte.
    4. compare lists the generated files that differ between two demo folders.
    Run: uv run --frozen python tools/build_demo.py [--out DIR] [--workspace DIR]

Guardrails
    - Offline and deterministic: the same fixtures and code give the same bytes, LF only, no
      temporary path inside any generated file.
    - A section that would be empty (no title family, no tier A or B role) stops the build
      with a non-zero exit that names the section, instead of publishing a hollow demo.
    - Generated files are written only by this tool. Casey's letter answers are the author's own
      in-character dictation and are only ever copied, never written or reworded here.
    - --workspace refuses a folder that already holds files, so it never overwrites real work.

QA checklist
    - Run it twice and run `git status --short demo`: the second run changes nothing.
    - Run `uv run --frozen pytest -q tests/test_demo.py`.
    - Leak-check a scratch copy of demo/ with `uv run --script tools/leak_check.py --root <dir>
      --require-denylist`.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jobsearch import cli, fetch, letters  # noqa: E402
from jobsearch.fetch import TransportResponse  # noqa: E402
from jobsearch.netguard import board_url  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"
PIPELINE = FIXTURES / "pipeline"
PERSONA = FIXTURES / "personas" / "casey-ops"
DEFAULT_OUT = REPO / "demo" / "casey-ops"

# The three clean fixture boards. The injection board is left out of the demo on purpose.
DEMO_BOARDS = {
    ("greenhouse", "northwind-example"): "greenhouse_northwind.json",
    ("lever", "harbor-example"): "lever_harbor.json",
    ("ashby", "meridian-example"): "ashby_meridian.json",
}
DEMO_DATE = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
SCAN_DATE = "2026-11-17"
# The role Casey writes the letter for: a tier A operations role on the fixture ranked list.
DEMO_LETTER_ROLE = "greenhouse:northwind-example:4100001"
KEEP_REASON = "Fits the operations work Casey is looking for."

LETTER_REL = letters.letter_rel(DEMO_LETTER_ROLE)
# Casey's letter inputs in demo/casey-ops/inputs -> the file letter-check reads in the letter folder.
LETTER_INPUTS = {
    "letter-answers.md": letters.ANSWERS_FILE,
    "letter-draft.md": letters.DRAFT_FILE,
    "letter-fixes.json": letters.FIXES_FILE,
}

# demo-relative path -> workspace-relative source. Sorted by demo path.
GENERATED_SOURCES = {
    "1-intake/answers.json": "profile/answers.json",
    "1-intake/criteria.json": "config/criteria.json",
    "1-intake/resume.md": "profile/resume.md",
    "1-intake/summary.md": "profile/summary.md",
    "2-titles/proposal-view.md": "titles/proposal-view.md",
    "2-titles/summary.md": f"titles/summary-{SCAN_DATE}.md",
    "3-ranked/funnel.md": f"ranked/funnel-{SCAN_DATE}.md",
    "3-ranked/ranked.md": f"ranked/ranked-{SCAN_DATE}.md",
    "4-letter/check.md": f"{LETTER_REL}/{letters.CHECK_FILE}",
    "4-letter/letter.md": f"{LETTER_REL}/{letters.LETTER_FILE}",
}
GENERATED_FILES = tuple(sorted(GENERATED_SOURCES))


def _transport(url, headers, clock=None):
    """Serve a fixture board body by its URL; anything else is a 404, never the network."""
    for (ats, slug), name in DEMO_BOARDS.items():
        if url == board_url(ats, slug):
            return TransportResponse(200, {"content-type": "application/json"},
                                     (PIPELINE / "http" / name).read_bytes())
    return TransportResponse(404, {"content-type": "application/json"}, b"{}")


def configure_offline(setattr_: Callable[[object, str, object], None], boards_dir: Path) -> None:
    """Apply the four offline patches through the setattr given (builtin or monkeypatch's)."""
    from jobsearch import clock, directory

    full = json.loads((PIPELINE / "boards.json").read_text(encoding="utf-8"))
    keep = {slug for _, slug in DEMO_BOARDS}
    full["entries"] = [e for e in full["entries"] if e["slug"] in keep]
    boards_dir.mkdir(parents=True, exist_ok=True)
    boards_file = boards_dir / "boards.json"
    boards_file.write_text(json.dumps(full, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    setattr_(directory, "DIRECTORY_PATH", boards_file)
    setattr_(clock, "now_utc", lambda: DEMO_DATE)
    setattr_(fetch, "_default_transport", _transport)
    setattr_(fetch, "MIN_INTERVAL_SECONDS", 0)
    fetch._reset_pacing()


def _stop(section: str, why: str) -> SystemExit:
    return SystemExit(f"Demo section '{section}' would be empty: {why}")


def _run(workspace: Path, *argv: str) -> dict:
    """Run one command the way the skills do and return its JSON line; stop if it failed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main([*argv, "--data-dir", str(workspace)])
    try:
        out = json.loads(buf.getvalue())
    except ValueError:
        raise SystemExit(f"{argv[0]} printed no JSON line: {buf.getvalue()[:200]}") from None
    if code != 0 or not out.get("ok"):
        raise SystemExit(f"{argv[0]} failed: {out.get('error')}")
    return out


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")


def _keep_every_listed_role(workspace: Path) -> int:
    """Write a keep verdict for every role on the worklist; return how many were added."""
    worklist = json.loads((workspace / "roles" / "function-worklist.json")
                          .read_text(encoding="utf-8"))
    verdicts_path = workspace / "roles" / "function-verdicts.json"
    if verdicts_path.is_file():
        data = json.loads(verdicts_path.read_text(encoding="utf-8"))
    else:
        data = {"schema_version": 1, "verdicts": []}
    seen = {v["role_id"] for v in data["verdicts"]}
    added = [r["role_id"] for r in worklist["roles"] if r["role_id"] not in seen]
    data["verdicts"] += [{"role_id": rid, "verdict": "keep", "reason": KEEP_REASON}
                         for rid in added]
    _write_json(verdicts_path, data)
    return len(added)


def build_workspace(workspace: Path, demo_root: Path, *, answers_update: dict | None = None) -> None:
    """Run the start skill's command order in `workspace`, reading agent steps from demo inputs."""
    inputs = demo_root / "inputs"
    _run(workspace, "init")
    _run(workspace, "profile-import", "--resume", str(PERSONA / "resume.md"))
    answers = json.loads((PERSONA / "answers.json").read_text(encoding="utf-8"))
    answers.update(answers_update or {})
    _write_json(workspace / "profile" / "answers.json", answers)
    _run(workspace, "criteria-build")
    _run(workspace, "sample")

    shutil.copyfile(inputs / "titles-proposal.json", workspace / "titles" / "proposal.json")
    _run(workspace, "titles-check")
    decisions = json.loads((inputs / "titles-decisions.json").read_text(encoding="utf-8"))
    if not any(d["decision"] in ("approve", "edit") for d in decisions["decisions"]):
        raise _stop("titles", "no title family was approved or edited.")
    shutil.copyfile(inputs / "titles-decisions.json", workspace / "titles" / "decisions.json")
    _run(workspace, "titles-apply")

    _run(workspace, "scan")
    preview = _run(workspace, "criteria-test")
    a_min, b_min = preview["data"].get("proposed_a_min"), preview["data"].get("proposed_b_min")
    if a_min is None or b_min is None:
        raise _stop("ranked", "no role passed the filters, so no cutoffs were proposed.")
    _run(workspace, "calibrate", "--a-min", str(a_min), "--b-min", str(b_min))

    for _ in range(3):
        review = _run(workspace, "function-review")
        if review["counts"]["to_review"] == 0:
            break
        _keep_every_listed_role(workspace)
    else:
        raise SystemExit("function-review still had roles to review after three passes.")
    _run(workspace, "rank")

    ranked = json.loads((workspace / "ranked" / f"ranked-{SCAN_DATE}.json")
                        .read_text(encoding="utf-8"))
    if not any(r["tier"] in ("A", "B") for r in ranked["roles"]):
        raise _stop("ranked", "no tier A or B role was found.")
    _build_letter(workspace, inputs)


def _build_letter(workspace: Path, inputs: Path) -> None:
    """Run letter-start and letter-check for Casey's role on her dictated answers and cut draft."""
    missing = [name for name in LETTER_INPUTS if not (inputs / name).is_file()]
    if missing:
        raise _stop("letter", f"{', '.join(missing)} is missing from demo/casey-ops/inputs.")
    _run(workspace, "letter-start", "--role", DEMO_LETTER_ROLE)
    for name, target in LETTER_INPUTS.items():
        shutil.copyfile(inputs / name, workspace / LETTER_REL / target)
    _run(workspace, "letter-check", "--role", DEMO_LETTER_ROLE)


def render_demo(workspace: Path, out: Path) -> None:
    """Copy every generated file from the workspace into the demo folder, byte for byte."""
    for rel, source in sorted(GENERATED_SOURCES.items()):
        src = workspace / source
        if not src.is_file():
            raise _stop(rel.split("/")[0], f"{source} was not written.")
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)


def compare(out_a: Path, out_b: Path) -> list[str]:
    """Return the generated files whose bytes differ, or that are missing from either side."""
    differing = []
    for rel in GENERATED_FILES:
        a, b = out_a / rel, out_b / rel
        if not (a.is_file() and b.is_file()) or a.read_bytes() != b.read_bytes():
            differing.append(rel)
    return differing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild the casey-ops demo offline.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="demo folder to write (default demo/casey-ops)")
    parser.add_argument("--workspace", type=Path, default=None,
                        help="keep the built data folder here (must be new or empty)")
    args = parser.parse_args(argv)
    if args.workspace is not None and args.workspace.exists() and any(args.workspace.iterdir()):
        print(f"The workspace folder {args.workspace} is not empty; pick a new one.",
              file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        configure_offline(setattr, scratch_path / "boards")
        workspace = args.workspace if args.workspace is not None else scratch_path / "ws"
        try:
            build_workspace(workspace, DEFAULT_OUT)
            render_demo(workspace, args.out)
        except SystemExit as stop:
            print(stop, file=sys.stderr)
            return 1
    print(f"Wrote {len(GENERATED_FILES)} files to {args.out}")
    if args.workspace is not None:
        print(f"Workspace kept at {args.workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
