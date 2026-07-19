"""Per-task reviewer scope: the reviewer sees only files changed during the
current task, and the dump is size-capped."""

from run_team import build_file_dump


def test_snapshot_diff_isolates_task_files(workspace):
    workspace.write_file("task1.py", "a = 1\n")
    snapshot = set(workspace.written_files)
    workspace.write_file("task2.py", "b = 2\n")
    changed = sorted(workspace.written_files - snapshot)
    assert changed == ["task2.py"]


def test_build_file_dump_contains_contents(workspace):
    workspace.write_file("a.py", "alpha = 1\n")
    workspace.write_file("b.py", "beta = 2\n")
    dump = build_file_dump(workspace, ["a.py", "b.py"])
    assert "### a.py" in dump and "alpha = 1" in dump
    assert "### b.py" in dump and "beta = 2" in dump


def test_build_file_dump_empty():
    class W:  # no reads should happen
        pass
    assert build_file_dump(W(), []) == "(no files were written)"


def test_build_file_dump_caps_total_size(workspace):
    big = "x = 'yyyyyyyyyy'\n" * 3000  # ~51k chars
    workspace.write_file("big.py", big)
    workspace.write_file("late.py", "z = 3\n")
    dump = build_file_dump(workspace, ["big.py", "late.py"], cap=30_000)
    assert "[... truncated for review size cap ...]" in dump
    assert "[omitted — review size cap reached]" in dump
    assert len(dump) < 32_000
