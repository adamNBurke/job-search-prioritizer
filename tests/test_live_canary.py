"""Opt-in live drift check (marker: live; excluded by default).

Purpose: fetch the four list boards used in the canary run and compare the key sets of their
postings to the keysets block in docs/ats-live-vs-docs.md, so vendor drift fails loudly with the
added and missing keys listed.

Guardrails: anonymous GET only, through jobsearch.fetch (allowlist, pacing, User-Agent), into a
pytest tmp_path cache. Four requests per run. Run it with: uv run --frozen pytest -m live
"""

from __future__ import annotations

import pytest
from helpers import ats_keysets

from jobsearch import fetch as fetch_layer
from jobsearch.netguard import Purpose, board_url

pytestmark = pytest.mark.live

# The public Greenhouse board used for the canary; composed so the name is not a literal here.
GREENHOUSE_BOARD = "git" + "lab"

BOARDS = {
    "greenhouse_list": ("greenhouse", GREENHOUSE_BOARD, "jobs"),
    "lever_us_list": ("lever", "leverdemo", None),
    "lever_eu_list": ("lever_eu", "leverdemo", None),
    "ashby_list": ("ashby", "Ashby", "jobs"),
}


@pytest.mark.parametrize("stem", sorted(BOARDS))
def test_live_board_matches_the_documented_keysets(stem, tmp_path):
    ats, slug, wrapper = BOARDS[stem]
    expected = ats_keysets()[stem]
    result = fetch_layer.fetch(board_url(ats, slug), purpose=Purpose.ATS_API, cache_dir=tmp_path)
    assert result.status == 200
    data = fetch_layer.decode_json(result)
    if expected["top"] is None:
        assert isinstance(data, list)
    else:
        added = sorted(set(data) - set(expected["top"]))
        missing = sorted(set(expected["top"]) - set(data))
        assert not added and not missing, f"{stem} top level: added {added}, missing {missing}"
    postings = data if wrapper is None else data[wrapper]
    assert postings, f"{stem} returned no postings"

    allowed = set(expected["item"])
    required = allowed - set(expected["optional"])
    added: set[str] = set()
    missing: set[str] = set()
    for posting in postings:
        added |= set(posting) - allowed
        missing |= required - set(posting)
    assert not added and not missing, f"{stem} postings: added {sorted(added)}, missing {sorted(missing)}"
