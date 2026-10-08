"""Static controls for the scaffold-only launch scripts (STACK-SCAFFOLD-R1,
option 3). File reads only: no script is imported or executed, and no
process, socket, journal or database is used.

- conformance/scaffold-scripts/ holds exactly the three scripts frozen from
  main e3dee31b (byte-identical, by sha256) plus its README;
- run-reference.sh reaches the launcher only through scaffold-scripts/, at
  exactly its two call sites;
- no strict tree (conformance/scripts except run-reference.sh,
  conformance/alrust_conformance, .github/workflows) names scaffold-scripts;
- the strict staged launcher still refuses the scaffold's environment
  secrets (its ALLOWED_ENV excludes DATABASE_URL and SECRET_KEY)."""
import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCAFFOLD = ROOT / "conformance" / "scaffold-scripts"
RUN_REFERENCE = ROOT / "conformance" / "scripts" / "run-reference.sh"
# sha256 of each file at main e3dee31b (conformance/scripts/).
FROZEN = {
    "launch-reference.py": "a5a63bfc697f2381",
    "reference-supervisor.py": "52418dba60b536c0",
    "stop-reference.py": "3714460e080cbec0",
}
STRICT_TREES = [ROOT / "conformance" / "scripts", ROOT / "conformance" / "alrust_conformance",
                ROOT / ".github" / "workflows"]


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_scaffold_directory_holds_exactly_the_frozen_scripts_and_readme():
    assert sorted(p.name for p in SCAFFOLD.iterdir()) == sorted(list(FROZEN) + ["README.md"])
    for name, prefix in FROZEN.items():
        assert _sha(SCAFFOLD / name).startswith(prefix), name


def test_the_scaffold_stop_helper_is_the_strict_one_byte_for_byte():
    # The stack never changed stop-reference.py, so ci.yml's stop step
    # (conformance/scripts/stop-reference.py) reads receipts written by the
    # same supervisor bytes as before.
    strict = ROOT / "conformance" / "scripts" / "stop-reference.py"
    assert _sha(SCAFFOLD / "stop-reference.py") == _sha(strict)


def test_run_reference_reaches_the_launcher_only_through_scaffold_scripts():
    text = RUN_REFERENCE.read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    calls = [line for line in code if "launch-reference.py" in line]
    assert len(calls) == 2
    assert all("$root/conformance/scaffold-scripts/launch-reference.py" in c for c in calls)
    assert sum("--preflight" in c for c in calls) == 1
    assert sum(c.lstrip().startswith("exec ") for c in calls) == 1
    assert "conformance/scripts/launch-reference.py" not in "\n".join(code)


def test_no_strict_tree_names_the_scaffold_scripts():
    found = []
    for tree in STRICT_TREES:
        for path in tree.rglob("*"):
            if path.is_file() and path != RUN_REFERENCE and path.suffix in (".py", ".sh", ".yml",
                                                                            ".yaml", ".json", ""):
                if "scaffold-scripts" in path.read_text(errors="replace"):
                    found.append(str(path.relative_to(ROOT)))
    assert found == []


def test_the_strict_launcher_still_refuses_environment_secrets():
    tree = ast.parse((ROOT / "conformance" / "scripts" / "launch-reference.py").read_text())
    [allowed] = [node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "ALLOWED_ENV" for t in node.targets)]
    names = {element.value for element in allowed.elts}
    assert "DATABASE_URL" not in names and "SECRET_KEY" not in names


def test_the_frozen_launcher_keeps_its_siblings_and_reference_root():
    # HERE-relative: the scaffold launcher uses the scaffold supervisor and
    # stop helper beside it, and reference/alerta/VERSION two levels up.
    text = (SCAFFOLD / "launch-reference.py").read_text()
    assert 'SUPERVISOR = os.path.join(HERE, "reference-supervisor.py")' in text
    assert 'STOP = os.path.join(HERE, "stop-reference.py")' in text
    # Checkout-independent (the reference submodule may be absent locally):
    # the launcher reads HERE/../../reference/alerta/VERSION, and the scaffold
    # directory sits at the same depth below the repository as scripts/.
    assert 'os.path.join(HERE, "..", "..", "reference", "alerta",' in text
    assert SCAFFOLD.parent.parent == (ROOT / "conformance" / "scripts").parent.parent == ROOT
