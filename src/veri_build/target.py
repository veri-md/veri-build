"""Target backend registry.

`compile_parent_subagent_runner.py` resolves a target string (e.g. ``dafny-rust``)
to a *target backend* via ``from veri_build.target import get``. A target backend
is toolchain glue — it knows which DSL language the runner should print/verify
against (``dsl_language``), how to extract the verified code to the output language
(``verify_extraction``), and supplies optional extra prompt rules for the fill
agent. This is distinct from the DSL-level ``Backend`` in ``dsl/src/backend/base.py``
(a parser+printer); the runner imports those printers/parsers itself, keyed off
``dsl_language()``, so this module needs no printer.

Scope: ``dafny-rust`` only. Sibling Dafny targets (java/js/python) differ only in
the ``dafny translate`` target and output suffix, so each is a one-line registry
addition.
"""

import shutil
import subprocess
from pathlib import Path

# Seconds allowed for `dafny translate` (matches pipeline.py:_compile_dafny_to_rust).
_TRANSLATE_TIMEOUT = 120


class DafnyBackend:
    """Toolchain glue for a ``dafny translate <lang>`` target."""

    def __init__(self, name: str, language: str,
                 translate_target: str, output_suffix: str):
        self.name = name                       # e.g. 'dafny-rust' (display)
        self.language = language               # e.g. 'Rust' (display)
        self._translate_target = translate_target  # dafny translate arg, e.g. 'rs'
        self._output_suffix = output_suffix         # output extension, no dot, e.g. 'rs'

    def dsl_language(self) -> str:
        return 'dafny'

    def output_suffix(self) -> str:
        # No leading dot: runner uses both f'*{suffix}' and f'*.{suffix}'.
        return self._output_suffix

    def verify_extraction(self, dfy_path, output_dir):
        """Extract verified Dafny to the output language via `dafny translate`.

        Mirrors pipeline.py:_compile_dafny_to_rust. Returns (ok, msg).
        """
        dafny = shutil.which('dafny')
        if not dafny:
            return (False, 'dafny not found in PATH')

        out = Path(output_dir) / self._translate_target
        out.mkdir(parents=True, exist_ok=True)
        module = Path(dfy_path).stem

        try:
            proc = subprocess.run(
                [dafny, 'translate', self._translate_target,
                 # Dafny's Rust backend rejects translation without this; it's a
                 # global option accepted by the other backends too.
                 '--enforce-determinism',
                 '--output', str(out / module), str(dfy_path)],
                capture_output=True, text=True, timeout=_TRANSLATE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (False, f'dafny translate {self._translate_target} '
                           f'timed out ({_TRANSLATE_TIMEOUT}s)')

        produced = list(out.rglob(f'*.{self._output_suffix}'))
        if produced:
            return (True, f'Extracted {self.language}: {produced[0].name}')
        detail = (proc.stderr or proc.stdout or '').strip()[:200]
        return (False, f'no .{self._output_suffix} produced: {detail}')

    def agent_extra_rules(self):
        return (
            "- Write pure Dafny `function` bodies as a single expression; "
            "no `method`, `:=`, `var`, loops, arrays, or `new`.\n"
            "- Keep each signature byte-identical to the interface."
        )

    def self_check_command(self):
        # write + `dafny verify` is already in the prompt; no extra step for v1.
        return None


_REGISTRY = {
    'dafny-rust': DafnyBackend('dafny-rust', 'Rust', 'rs', 'rs'),
}

# The runner passes the raw `--target` value (e.g. 'dafny') into its inner
# functions, not the alias-resolved canonical name, so the registry must
# canonicalize itself. Mirrors the alias map in compile_parent_subagent_runner.py.
_ALIASES = {
    'dafny': 'dafny-rust',
    'rust': 'dafny-rust',
}


def get(target: str):
    """Resolve a target string (canonical or alias) to its backend.

    Raises KeyError on unknown targets — the runner catches it and reports a
    clean "Unknown target" error.
    """
    key = target.lower().strip()
    key = _ALIASES.get(key, key)
    return _REGISTRY[key]
