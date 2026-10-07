from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tooling" / "architecture"))
SPEC = importlib.util.spec_from_file_location("module_registry", ROOT / "tooling" / "architecture" / "validate-module-registry.py")
REGISTRY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(REGISTRY)


def manifest(*modules: dict[str, object]) -> dict[str, object]:
    return {"version": 1, "modules": list(modules)}


def module(identifier: str, root: str, dependencies: list[str] | None = None) -> dict[str, object]:
    return {
        "id": identifier,
        "root": root,
        "kind": "python",
        "public_api": root,
        "python_modules": [],
        "dependencies": dependencies or [],
    }


class ModuleRegistryTests(unittest.TestCase):
    def test_integrated_registry_is_valid(self):
        self.assertEqual([], REGISTRY.check())

    def test_missing_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = REGISTRY.validate_manifest(manifest(module("a", "missing")), root)
        self.assertTrue(any("does not exist" in error for error in errors))

    def test_outside_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = REGISTRY.validate_manifest(manifest(module("a", "../outside-root")), root)
        self.assertTrue(any("within the repository" in error for error in errors))

    def test_duplicate_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            errors = REGISTRY.validate_manifest(manifest(module("a", "src"), module("b", "src")), root)
        self.assertTrue(any("duplicate module root" in error for error in errors))

    def test_unknown_dependency_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            errors = REGISTRY.validate_manifest(manifest(module("a", "src", ["missing"])), root)
        self.assertTrue(any("unknown dependency" in error for error in errors))

    def test_duplicate_id_and_self_dependency_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            errors = REGISTRY.validate_manifest(manifest(module("a", "src", ["a"]), module("a", "other")), root)
        self.assertTrue(any("duplicate module id" in error for error in errors))
        self.assertTrue(any("cannot depend on itself" in error for error in errors))

    def test_unsupported_kind_and_public_api_escape_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "other").mkdir()
            invalid = module("a", "src")
            invalid["kind"] = ""
            invalid["public_api"] = "other"
            errors = REGISTRY.validate_manifest(manifest(invalid), root)
        self.assertTrue(any("kind must be a non-empty" in error for error in errors))
        self.assertTrue(any("public_api must be within" in error for error in errors))

    def test_imports_between_registered_python_modules_require_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("import aura_core.example\n", encoding="utf-8")
            (root / "b.py").write_text("# target\n", encoding="utf-8")
            source = module("a", "a.py")
            source["python_modules"] = ["aura_core.source"]
            target = module("b", "b.py")
            target["python_modules"] = ["aura_core.example"]
            errors = REGISTRY.validate_manifest(manifest(source, target), root)
        self.assertTrue(any("without declaring the dependency" in error for error in errors))

    def test_omitted_core_source_file_cannot_bypass_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root = root / "services" / "aura-core" / "src" / "aura_core" / "example"
            package_root.mkdir(parents=True)
            (package_root / "__init__.py").write_text("\"\"\"Example package.\"\"\"\n", encoding="utf-8")
            (package_root / "present.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package_root / "omitted.py").write_text("VALUE = 2\n", encoding="utf-8")
            errors = REGISTRY.validate_manifest(
                manifest(
                    {
                        "id": "example-package",
                        "root": "services/aura-core/src/aura_core/example",
                        "kind": "package",
                        "public_api": "services/aura-core/src/aura_core/example/present.py",
                        "python_modules": ["aura_core.example"],
                        "source_files": [
                            "services/aura-core/src/aura_core/example/__init__.py"
                        ],
                        "dependencies": [],
                    },
                    {
                        "id": "example-present",
                        "root": "services/aura-core/src/aura_core/example/present.py",
                        "kind": "python",
                        "public_api": "services/aura-core/src/aura_core/example/present.py",
                        "python_modules": ["aura_core.example.present"],
                        "dependencies": [],
                    },
                ),
                root,
            )
        self.assertTrue(any("omitted.py" in error and "found none" in error for error in errors))

    def test_relative_imports_require_declared_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("from .target import VALUE\n", encoding="utf-8")
            (root / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
            source = module("source", "source.py")
            source["python_modules"] = ["aura_core.example.source"]
            target = module("target", "target.py")
            target["python_modules"] = ["aura_core.example.target"]
            errors = REGISTRY.validate_manifest(manifest(source, target), root)
        self.assertTrue(any("without declaring the dependency" in error for error in errors))

    def test_escaping_relative_import_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("from ...outside import VALUE\n", encoding="utf-8")
            source = module("source", "source.py")
            source["python_modules"] = ["aura_core.example.source"]
            errors = REGISTRY.validate_manifest(manifest(source), root)
        self.assertTrue(any("escapes module package" in error for error in errors))

    def test_dependency_rule_metadata_types_fail_closed(self):
        errors = REGISTRY.validate_dependency_rules(
            {
                "rules": [
                    {
                        "id": "bad",
                        "source": "../outside/**",
                        "policy": "",
                        "forbidden_imports": "not-a-list",
                        "require_cross_domain_public_api": "yes",
                    }
                ]
            }
        )
        self.assertTrue(any("nonescaping" in error for error in errors))
        self.assertTrue(any("policy must be a non-empty" in error for error in errors))
        self.assertTrue(any("forbidden_imports must be" in error for error in errors))
        self.assertTrue(any("must be boolean" in error for error in errors))

    def test_cli_always_emits_json_for_malformed_missing_relative_and_outside_inputs(self):
        validator = ROOT / "tooling" / "architecture" / "validate-module-registry.py"

        def run(*args: str) -> tuple[int, dict[str, object]]:
            result = subprocess.run(
                ["python3", str(validator), *args],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            return result.returncode, json.loads(result.stdout)

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            malformed = temporary / "malformed.yaml"
            malformed.write_text("modules: [", encoding="utf-8")
            outside = temporary / "outside.yaml"
            outside.write_text(
                "version: 1\nmodules:\n  - id: x\n    root: ../outside\n    kind: python\n    public_api: ../outside\n    python_modules: []\n    dependencies: []\n",
                encoding="utf-8",
            )
            missing_code, missing_payload = run("--manifest", str(temporary / "missing.yaml"))
            malformed_code, malformed_payload = run("--manifest", str(malformed))
            outside_code, outside_payload = run("--manifest", str(outside))
            relative_code, relative_payload = run("--manifest", "tooling/architecture/modules.yaml")

        self.assertNotEqual(0, missing_code)
        self.assertNotEqual(0, malformed_code)
        self.assertNotEqual(0, outside_code)
        self.assertEqual(0, relative_code)
        for payload in (missing_payload, malformed_payload, outside_payload, relative_payload):
            self.assertEqual("module-registry", payload["check"])
            self.assertIn(payload["status"], {"passed", "failed"})

    def test_dependency_rules_require_restrictions(self):
        errors = REGISTRY.validate_dependency_rules({"rules": [{"id": "x", "source": "src/**"}]})
        self.assertTrue(any("policy or restriction" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
