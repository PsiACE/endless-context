#!/usr/bin/env python3
"""Copy Bub built-in skills into workspace so they are discoverable and runnable.

Bub discovers skills from workspace/.agent/skills (project), then global, then
builtin (bub package). As a distribution we run with workspace=/app; the
package-builtin skills are under the installed bub path, but when tools/skills
run scripts they often use workspace-relative paths. So we copy the **entire**
builtin skill directories (SKILL.md, scripts/, etc.) into workspace/.agent/skills/
so they behave as project skills and script paths resolve correctly.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJECT_SKILLS_DIR = ".agent/skills"


def main() -> int:
    workspace = Path(os.getenv("BUB_WORKSPACE_PATH", "/app")).resolve()
    skills_dst = workspace / PROJECT_SKILLS_DIR

    try:
        import bub
    except ImportError:
        print("setup-bub-workspace: bub not installed, skip", file=sys.stderr)
        return 0

    bub_root = Path(bub.__path__[0]).resolve()
    skills_src = bub_root / "skills"
    if not skills_src.is_dir():
        return 0

    skills_dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("_"):
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        dest = skills_dst / skill_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest, dirs_exist_ok=False)
        copied += 1

    if copied:
        print(f"setup-bub-workspace: copied {copied} skill(s) to {skills_dst}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
