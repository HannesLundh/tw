"""Environment fact-checking: BLOCKED detection, PATH verification, the
error-line extractor, and the independent final-verify override."""

import shutil

import pytest

from run_team import (
    find_blocked, installed_tools_in, preflight_tools, error_snippet,
    apply_final_verify,
)


def test_find_blocked_variants():
    assert find_blocked("BLOCKED: dotnet is not installed; needed to build")
    assert find_blocked("## BLOCKED: func CLI missing")
    assert find_blocked("**BLOCKED: no compiler** rest")
    assert find_blocked("Summary:\nBLOCKED: no compiler")


def test_find_blocked_negatives():
    assert find_blocked("All good, tests pass") is None
    assert find_blocked("intro\na\nb\nc\nd\nBLOCKED: too deep") is None


def test_installed_tools_in_hyphen_compound():
    # 'dotnet-isolated runtime' is really a claim about dotnet
    real = "make" if shutil.which("make") else None
    if not real:
        pytest.skip("no known toolchain binary present to assert on")
    hit = installed_tools_in(f"BLOCKED: {real}-toolchain runtime is not installed")
    assert real in hit


def test_installed_tools_in_no_false_match():
    # 'func' must not match 'functions'; absent tools must not appear
    assert installed_tools_in("BLOCKED: the Azure Functions bundle is missing") == {}
    assert installed_tools_in("BLOCKED: cannot install or test anything") == {}


def test_preflight_tools_detects_missing():
    found, missing = preflight_tools("use 'func init' then 'dotnet build'")
    # neither is on a CI runner's PATH normally; at minimum they are classified
    assert set(found) | set(missing) >= {"func", "dotnet"}
    for t in ("func", "dotnet"):
        assert (t in found) ^ (t in missing)


def test_preflight_no_substring_false_hit():
    found, missing = preflight_tools("build an azure function with dotnet-isolated runtime")
    # 'function'/'dotnet-isolated' must not spuriously add 'func' as its own token twice
    assert "function" not in found and "function" not in missing


def test_error_snippet_prefers_error_line():
    out = ("exit code: 1\nstdout:\ninfo : restoring packages\n"
           "info : X.509 chain\nerror: NU1101: package not found\n")
    assert error_snippet(out).startswith("error: NU1101")


def test_error_snippet_falls_back_to_last_line():
    out = "exit code: 1\nstdout:\nCreating template...\nOverwrite? [y/n]\n"
    assert error_snippet(out) == "Overwrite? [y/n]"


def test_apply_final_verify_passthrough_when_no_command(workspace):
    passed, report = apply_final_verify(True, "RESULT: PASS", workspace, None)
    assert passed is True and "RESULT: PASS" in report


def test_apply_final_verify_overrides_false_pass(workspace):
    passed, report = apply_final_verify(True, "RESULT: PASS", workspace, "false")
    assert passed is False
    assert "INDEPENDENT VERIFICATION FAILED" in report


def test_apply_final_verify_confirms_real_pass(workspace):
    passed, report = apply_final_verify(True, "RESULT: PASS", workspace, "true")
    assert passed is True


def test_classify_missing_quoted_is_hard():
    from run_team import classify_missing
    hard, soft = classify_missing(
        "scaffold with 'func init --worker-runtime dotnet-isolated' please",
        ["func", "docker"])
    assert hard == ["func"] and soft == ["docker"]


def test_classify_missing_prose_is_soft():
    from run_team import classify_missing
    hard, soft = classify_missing("build this without docker or make", ["docker", "make"])
    assert hard == [] and set(soft) == {"docker", "make"}
