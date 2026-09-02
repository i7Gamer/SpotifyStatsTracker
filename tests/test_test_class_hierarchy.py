"""No test class inherits from a sibling that defines tests.

A subclass runs every test_ method it inherits, so a class that subclasses a
sibling for its setUp and helpers runs that sibling's tests again under its
own name. Twice this repo paid for it: the 12 end-time sweep tests ran three
times and the 14 tag-route tests twice - 38 redundant executions per run, and
a count read from either file overstated what it covered. Shared fixtures
belong in a base that defines NO tests (the `_SweepBase` / `_AppTestBase`
shape), which is what this pins.

Structural, because nothing behavioural notices: the duplicated tests pass
the second time exactly as they passed the first.
"""
import ast
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _testClassesByName(tree):
    """{className: {test method names}} for every class in the module."""
    return {
        node.name: {
            item.name for item in node.body
            if isinstance(item, ast.FunctionDef) and item.name.startswith("test_")
        }
        for node in tree.body if isinstance(node, ast.ClassDef)
    }


def _offenders(path):
    """(subclass, base) pairs where base is a class in the same module that
    defines test_ methods of its own."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = _testClassesByName(tree)
    found = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and classes.get(base.id):
                found.append((node.name, base.id))
    return found


class TestNoTestClassInheritsAnotherClassesTests(unittest.TestCase):
    def test_every_test_runs_under_one_class_only(self):
        offenders = [
            f"{path.name}: {sub} subclasses {base}, which defines tests"
            for path in sorted(TESTS_DIR.glob("test_*.py"))
            for sub, base in _offenders(path)
        ]

        self.assertEqual(
            offenders, [],
            "move the shared setUp/helpers into a base class with no tests:\n"
            + "\n".join(offenders))

    def test_the_scan_sees_a_test_class(self):
        """Guards the gate above: a scan that found no classes anywhere would
        pass by finding nothing."""
        self.assertIn("TestNoTestClassInheritsAnotherClassesTests",
                      _testClassesByName(ast.parse(Path(__file__).read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main()
