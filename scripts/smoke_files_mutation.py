"""Phase 26 (Workstream 1) — regression coverage for the new filesystem mutation
skills (`files.write`, `files.copy`, `files.move`, `files.delete`, `files.mkdir`).

The completion audit found FRIDAY could search/read/reveal files but had no
first-class way to create, copy, move, or delete one — every such request had
to go through `shell.run`, bypassing the per-call risk classifiers and
postcondition verifiers the rest of the skill layer relies on (see
friday/skills/files.py's module docstring and friday/verify.py's new
`_fs_*` verifiers).

This suite pins, all against a real throwaway temp directory (no mocks needed —
these are real, safe, local filesystem operations):

  A  files.write creates a new file; content matches; re-reading confirms it
  B  files.write's risk classifier escalates only when the target already
     exists (browser.click's is_consequential pattern, reused here)
  C  files.copy duplicates a file; the original is untouched; copying to a
     nonexistent parent folder is rejected cleanly
  D  files.move renames/relocates a file; the old path is gone, the new one
     exists; moving a nonexistent file is rejected cleanly
  E  files.delete removes a file; refuses a non-empty folder; deletes an
     empty folder; a path that doesn't exist is rejected cleanly
  F  files.mkdir creates a folder; refuses to recreate an existing one
  G  the new verify.py verifiers read real post-state back correctly
     (verified/failed, not just "the tool said so")
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday import verify  # noqa: E402
from friday.skills import files  # noqa: E402


def main() -> int:
    t0 = time.perf_counter()

    import tempfile

    with tempfile.TemporaryDirectory(prefix="friday_fs_smoke_") as tmp:
        root = Path(tmp)

        scenario("A: files.write creates a new file with the given content")
        target = root / "notes.txt"
        r = files.write(path=str(target), content="hello from phase 26")
        check("write reports ok", r.ok, r.speech)
        check("write says 'Created'", "created" in r.speech.lower(), r.speech)
        check("the file now exists on disk", target.exists(), str(target))
        check("the content matches", target.read_text(encoding="utf-8") == "hello from phase 26")
        check("data reports overwritten=False for a fresh file", r.data.get("overwritten") is False, r.data)

        no_parent = root / "nonexistent_folder" / "x.txt"
        r_bad = files.write(path=str(no_parent), content="x")
        check("write to a missing parent folder is rejected cleanly", not r_bad.ok, r_bad.speech)
        check("...and nothing was written", not no_parent.exists())

        scenario("B: files.write's risk classifier escalates only for an existing target")
        check("a fresh path is NOT flagged as risky", files._overwrites(str(root / "brand_new.txt")) is False)
        check("an existing path IS flagged as risky (would overwrite)", files._overwrites(str(target)) is True)

        r_over = files.write(path=str(target), content="replaced")
        check("overwriting an existing file still succeeds at the skill layer (risk is a permissions-layer concern)", r_over.ok, r_over.speech)
        check("...and says 'Overwrote', not 'Created'", "overwrote" in r_over.speech.lower(), r_over.speech)
        check("data reports overwritten=True", r_over.data.get("overwritten") is True, r_over.data)

        scenario("C: files.copy duplicates a file; original untouched; bad destination rejected")
        src = root / "source.txt"
        src.write_text("copy me", encoding="utf-8")
        dst = root / "copy_of_source.txt"
        rc = files.copy(src=str(src), dst=str(dst))
        check("copy reports ok", rc.ok, rc.speech)
        check("the copy exists", dst.exists())
        check("the copy's content matches the source", dst.read_text(encoding="utf-8") == "copy me")
        check("the original source is untouched", src.exists() and src.read_text(encoding="utf-8") == "copy me")

        rc_bad = files.copy(src=str(root / "does_not_exist.txt"), dst=str(root / "y.txt"))
        check("copying a nonexistent source is rejected cleanly", not rc_bad.ok, rc_bad.speech)

        rc_bad_dst = files.copy(src=str(src), dst=str(root / "no_such_folder" / "y.txt"))
        check("copying into a nonexistent destination folder is rejected cleanly", not rc_bad_dst.ok, rc_bad_dst.speech)

        scenario("D: files.move relocates/renames a file; old path gone, new path exists")
        mv_src = root / "to_move.txt"
        mv_src.write_text("move me", encoding="utf-8")
        mv_dst = root / "moved.txt"
        rm = files.move(src=str(mv_src), dst=str(mv_dst))
        check("move reports ok", rm.ok, rm.speech)
        check("the old path is gone", not mv_src.exists())
        check("the new path exists with the same content", mv_dst.exists() and mv_dst.read_text(encoding="utf-8") == "move me")

        rm_bad = files.move(src=str(root / "ghost.txt"), dst=str(root / "z.txt"))
        check("moving a nonexistent file is rejected cleanly", not rm_bad.ok, rm_bad.speech)

        scenario("E: files.delete removes a file; refuses a non-empty folder; deletes an empty one")
        doomed = root / "doomed.txt"
        doomed.write_text("bye", encoding="utf-8")
        rd = files.delete(path=str(doomed))
        check("delete reports ok", rd.ok, rd.speech)
        check("the file is really gone", not doomed.exists())

        rd_missing = files.delete(path=str(root / "already_gone.txt"))
        check("deleting a nonexistent path is rejected cleanly", not rd_missing.ok, rd_missing.speech)

        full_dir = root / "full_dir"
        full_dir.mkdir()
        (full_dir / "inside.txt").write_text("stuff", encoding="utf-8")
        rd_full = files.delete(path=str(full_dir))
        check("deleting a non-empty folder is refused (no recursive delete)", not rd_full.ok, rd_full.speech)
        check("...the folder and its contents are untouched", full_dir.exists() and (full_dir / "inside.txt").exists())

        empty_dir = root / "empty_dir"
        empty_dir.mkdir()
        rd_empty = files.delete(path=str(empty_dir))
        check("deleting an empty folder succeeds", rd_empty.ok, rd_empty.speech)
        check("...and it's really gone", not empty_dir.exists())

        scenario("F: files.mkdir creates a folder; refuses to recreate an existing one")
        new_dir = root / "brand_new_dir"
        rmk = files.mkdir(path=str(new_dir))
        check("mkdir reports ok", rmk.ok, rmk.speech)
        check("the folder now exists", new_dir.is_dir())

        rmk_dup = files.mkdir(path=str(new_dir))
        check("recreating an existing folder is rejected cleanly", not rmk_dup.ok, rmk_dup.speech)

        rmk_bad = files.mkdir(path=str(root / "no_such_parent" / "child"))
        check("mkdir under a missing parent is rejected cleanly", not rmk_bad.ok, rmk_bad.speech)

        scenario("G: the new verify.py verifiers read real post-state back")
        v_write = verify.VERIFIERS["files.write"]
        checks = v_write.check({"path": str(target), "content": "replaced"}, {"path": str(target)}, {})
        check("files.write verifier confirms the file exists with matching content", all(c.passed for c in checks), checks)

        v_delete = verify.VERIFIERS["files.delete"]
        checks_del = v_delete.check({"path": str(doomed)}, {"path": str(doomed)}, {})
        check("files.delete verifier confirms the path is gone", all(c.passed for c in checks_del), checks_del)

        v_mkdir = verify.VERIFIERS["files.mkdir"]
        checks_mk = v_mkdir.check({"path": str(new_dir)}, {"path": str(new_dir)}, {})
        check("files.mkdir verifier confirms the folder exists", all(c.passed for c in checks_mk), checks_mk)

        v_move = verify.VERIFIERS["files.move"]
        checks_mv = v_move.check({"src": str(mv_src), "dst": str(mv_dst)}, {"src": str(mv_src), "dst": str(mv_dst)}, {})
        check("files.move verifier confirms old-gone/new-exists", all(c.passed for c in checks_mv), checks_mv)

        v_copy = verify.VERIFIERS["files.copy"]
        checks_cp = v_copy.check({"src": str(src), "dst": str(dst)}, {"src": str(src), "dst": str(dst)}, {})
        check("files.copy verifier confirms the copy exists", all(c.passed for c in checks_cp), checks_cp)

    return finish("Phase 26 — filesystem mutation skills", time.perf_counter() - t0, min_assertions=30, min_scenarios=7)


if __name__ == "__main__":
    sys.exit(main())
