"""
target.py — Registry of Veri DSL compilation targets/backends.

Provides BackendTarget objects with the methods needed by
compile_parent_subagent_runner.py and other pipeline components.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class BackendTarget:
    """A compilation target = verifier + code generator + agent configuration.

    This is the concrete backend metadata that the compile pipeline uses,
    layered on top of the base Backend (parser+printer) classes.
    """
    name: str                     # short name: "fstar-c", "dafny-rust"
    language: str                 # human language: "F*", "Dafny"
    dsl_lang: str                 # underlying DSL: "fstar", "dafny", "python"
    suffix: str                   # ".c", ".rs", ".ml", ".py"
    agent_extra: str = ""         # extra rules for agent prompt
    self_check: str = ""          # self-check command for agent
    verify_extraction_func: callable = None  # (Path, Path) -> (bool, str)

    def dsl_language(self) -> str:
        return self.dsl_lang

    def agent_extra_rules(self) -> str:
        return self.agent_extra

    def self_check_command(self) -> str:
        return self.self_check

    def output_suffix(self) -> str:
        return self.suffix

    def verify_extraction(self, source_path: Path, output_dir: Path) -> Tuple[bool, str]:
        if self.verify_extraction_func:
            return self.verify_extraction_func(source_path, output_dir)
        return False, "No extraction verification function registered"


# ── Extraction verification helpers ──────────────────────────────────────

def _fstar_krml_verify(source_path: Path, output_dir: Path) -> Tuple[bool, str]:
    """Verify F* extraction via KaRaMeL."""
    import subprocess
    module_name = source_path.stem

    try:
        # Extract KaRaMeL IR
        result = subprocess.run(
            ['fstar.exe', '--codegen', 'krml', '--extract', module_name,
             '--admit_smt_queries', 'true',
             '--odir', str(output_dir),
             str(source_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return False, f"KaRaMeL extraction failed: {result.stderr[:300]}"

        # Generate C
        krml_out = output_dir / f'{module_name}.krml'
        if not krml_out.exists():
            return False, "KaRaMeL IR file not found"

        c_dir = output_dir / 'c'
        c_dir.mkdir(exist_ok=True)
        result = subprocess.run(
            ['krml', '-skip-compilation', str(krml_out), '-tmpdir', str(c_dir)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return False, f"C generation failed: {result.stderr[:300]}"

        # Find output .c file
        for cf in output_dir.rglob('*.c'):
            if cf.name.startswith(module_name) or cf.name == f'{module_name}.c':
                return True, f"C output: {cf}"
        for cf in c_dir.glob('*.c'):
            return True, f"C output: {cf}"
        return False, "No .c file found"

    except Exception as e:
        return False, str(e)


def _dafny_rust_verify(source_path: Path, output_dir: Path) -> Tuple[bool, str]:
    """Verify Dafny extraction to Rust."""
    import subprocess

    try:
        dafny_out = output_dir / 'rust'
        dafny_out.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ['dafny', 'translate', 'rs',
             '--output', str(dafny_out / source_path.stem),
             str(source_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return False, f"Dafny Rust extraction failed: {result.stderr[:300]}"

        for rf in dafny_out.glob('*.rs'):
            return True, f"Rust output: {rf}"
        return False, "No .rs file found"

    except Exception as e:
        return False, str(e)


# ── Backend registry ─────────────────────────────────────────────────────

BACKENDS = {
    "fstar-c": BackendTarget(
        name="fstar-c",
        language="F*",
        dsl_lang="fstar",
        suffix=".c",
        agent_extra="",
        self_check="",
        verify_extraction_func=_fstar_krml_verify,
    ),
    "fstar-ocaml": BackendTarget(
        name="fstar-ocaml",
        language="F*",
        dsl_lang="fstar",
        suffix=".ml",
        agent_extra="",
        self_check="",
    ),
    "fstar-wasm": BackendTarget(
        name="fstar-wasm",
        language="F*",
        dsl_lang="fstar",
        suffix=".c",
        agent_extra="",
        self_check="",
        verify_extraction_func=_fstar_krml_verify,
    ),
    "dafny-rust": BackendTarget(
        name="dafny-rust",
        language="Dafny",
        dsl_lang="dafny",
        suffix=".rs",
        agent_extra="",
        self_check="",
        verify_extraction_func=_dafny_rust_verify,
    ),
    "dafny-java": BackendTarget(
        name="dafny-java",
        language="Dafny",
        dsl_lang="dafny",
        suffix=".java",
        agent_extra="",
        self_check="",
    ),
    "dafny-js": BackendTarget(
        name="dafny-js",
        language="Dafny",
        dsl_lang="dafny",
        suffix=".js",
        agent_extra="",
        self_check="",
    ),
    "python-assert": BackendTarget(
        name="python-assert",
        language="Python",
        dsl_lang="python",
        suffix=".py",
        agent_extra="",
        self_check="",
    ),
}

# Aliases for backward compatibility
ALIASES = {
    "fstar": "fstar-c", "f-star": "fstar-c", "c": "fstar-c",
    "ocaml": "fstar-ocaml", "ml": "fstar-ocaml",
    "wasm": "fstar-wasm", "f-star-wasm": "fstar-wasm",
    "dafny": "dafny-rust", "rust": "dafny-rust",
    "java": "dafny-java",
    "js": "dafny-js", "javascript": "dafny-js",
    "python": "python-assert", "py": "python-assert",
}


def get(target_name: str) -> BackendTarget:
    """Resolve a target name to a BackendTarget.

    Handles aliases (e.g., 'c' -> 'fstar-c', 'wasm' -> 'fstar-wasm').
    Raises KeyError if unknown.
    """
    resolved = ALIASES.get(target_name.lower().strip(), target_name)
    if resolved not in BACKENDS:
        known = ", ".join(sorted(BACKENDS.keys()))
        raise KeyError(
            f"Unknown target '{target_name}' (resolved: '{resolved}'). "
            f"Known targets: {known}"
        )
    return BACKENDS[resolved]


def register(name: str, target: BackendTarget):
    """Register a custom backend target."""
    BACKENDS[name] = target


def known_targets() -> list:
    """Return list of registered target names."""
    return sorted(BACKENDS.keys())
