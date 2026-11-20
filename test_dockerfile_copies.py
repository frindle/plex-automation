"""Every local module the image's code imports must be COPYed by the Dockerfile.

The docker-build CI job only builds the image; it never imports the app, so a missing
COPY line only shows up as a crash when the container starts. This walks the imports of
each COPYed .py file (including imports inside functions) and fails on any repo-root
module that is imported but not copied. Run: python test_dockerfile_copies.py
"""
import ast
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def copied_py(dockerfile):
    out = set()
    for line in dockerfile.splitlines():
        m = re.match(r"^\s*COPY\s+(.+?)\s+\S+\s*$", line)
        if m:
            out.update(p for p in m.group(1).split() if p.endswith(".py"))
    return out


def local_imports(source, local):
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        else:
            continue
        found.update(n for n in names if n in local)
    return found


def missing_copies(root=ROOT):
    with open(os.path.join(root, "Dockerfile")) as f:
        copied = copied_py(f.read())
    local = {f[:-3] for f in os.listdir(root) if f.endswith(".py")}
    missing = {}
    for py in sorted(copied):
        with open(os.path.join(root, py)) as f:
            need = {m + ".py" for m in local_imports(f.read(), local)} - copied
        if need:
            missing[py] = sorted(need)
    return missing


class DockerfileCopies(unittest.TestCase):
    def test_every_imported_local_module_is_copied(self):
        self.assertEqual(missing_copies(), {}, "add COPY lines to the Dockerfile for these modules")

    def test_detects_a_missing_copy(self):
        self.assertEqual(local_imports("import a\ndef f():\n    from b import c\nimport os\n", {"a", "b"}), {"a", "b"})
        self.assertEqual(copied_py("COPY x.py .\nCOPY a.py b.py ./\nCOPY requirements.txt .\n"), {"x.py", "a.py", "b.py"})


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
