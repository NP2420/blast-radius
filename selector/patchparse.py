"""Unified diff parsing and application (stdlib only)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # with ' ', '-', '+' prefixes


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    rename: bool = False
    mode_change: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None

    def removed_old_lines(self) -> list[int]:
        """Old line numbers that are deleted or replaced."""
        out = []
        for h in self.hunks:
            o = h.old_start
            for ln in h.lines:
                tag = ln[:1]
                if tag == "-":
                    out.append(o)
                    o += 1
                elif tag == " ":
                    o += 1
        return out

    def insertion_points(self) -> list[tuple[int, list[int]]]:
        """(old line after which text is inserted, new line numbers inserted).

        Only pure insertions: '+' runs not directly replacing '-' lines.
        """
        out = []
        for h in self.hunks:
            o, n = h.old_start, h.new_start
            prev_minus = False
            run: list[int] = []
            run_after = 0
            for ln in h.lines + [" "]:
                tag = ln[:1]
                if tag == "+":
                    if not run:
                        run_after = o - 1
                    run.append(n)
                    n += 1
                    continue
                if run:
                    if not prev_minus:
                        out.append((run_after, run))
                    run = []
                if tag == "-":
                    prev_minus = True
                    o += 1
                elif tag == " ":
                    prev_minus = False
                    o += 1
                    n += 1
        return out

    def added_new_lines(self) -> list[int]:
        out = []
        for h in self.hunks:
            n = h.new_start
            for ln in h.lines:
                tag = ln[:1]
                if tag == "+":
                    out.append(n)
                    n += 1
                elif tag == " ":
                    n += 1
        return out


def _strip_path(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            cur = FileDiff(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            hunk = None
        elif cur is not None and hunk is None and ln.startswith("--- "):
            cur.old_path = _strip_path(ln[4:])
        elif cur is not None and hunk is None and ln.startswith("+++ "):
            cur.new_path = _strip_path(ln[4:])
        elif cur is not None and ln.startswith(("new file mode", "deleted file mode")) and hunk is None:
            if ln.startswith("new"):
                cur.old_path = None
            else:
                cur.new_path = None
        elif cur is not None and hunk is None and ln.startswith(("rename from", "rename to", "copy from", "copy to", "similarity index")):
            cur.rename = True
        elif cur is not None and hunk is None and ln.startswith(("old mode", "new mode")):
            cur.mode_change = True
        elif cur is not None and (ln.startswith("Binary files") or ln.startswith("GIT binary patch")):
            cur.binary = True
        elif cur is not None and ln.startswith("@@"):
            m = _HUNK.match(ln)
            if m:
                hunk = Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1))
                cur.hunks.append(hunk)
                # read exactly the counted lines
                o_left, n_left = hunk.old_len, hunk.new_len
                i += 1
                while i < len(lines) and (o_left > 0 or n_left > 0):
                    body = lines[i]
                    tag = body[:1]
                    if tag == "\\":  # "\ No newline at end of file"
                        i += 1
                        continue
                    if tag == "-":
                        o_left -= 1
                    elif tag == "+":
                        n_left -= 1
                    elif tag == " " or body == "":
                        body = " " + body[1:] if body else " "
                        o_left -= 1
                        n_left -= 1
                    else:
                        break
                    hunk.lines.append(body)
                    i += 1
                # skip a trailing "\ No newline" marker
                while i < len(lines) and lines[i].startswith("\\"):
                    i += 1
                hunk = None
                continue
        i += 1
    return files


class ApplyError(Exception):
    pass


def apply(old_text: str, fd: FileDiff) -> str:
    """Apply one file's hunks to old_text; exact line numbers, verified context."""
    old = old_text.split("\n")
    out: list[str] = []
    pos = 0  # index into old (0-based)
    for h in fd.hunks:
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        if start < pos:
            raise ApplyError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        for ln in h.lines:
            tag, body = ln[:1], ln[1:]
            if tag in (" ", "-"):
                if pos >= len(old) or old[pos] != body:
                    raise ApplyError(f"context mismatch at old line {pos + 1}")
                if tag == " ":
                    out.append(body)
                pos += 1
            elif tag == "+":
                out.append(body)
    out.extend(old[pos:])
    return "\n".join(out)
