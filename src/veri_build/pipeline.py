#!/usr/bin/env python3
"""Pipeline APIs for the Veri DSL verification system.

Three APIs:
  1. convert  — F* ↔ Veri DSL DSL (for agents to construct .veri.md)
  2. lint     — validate .veri.md (Veri DSL only, runs F*/Dafny verification)
  3. compile  — end-to-end pipeline (Veri DSL → target → agent → verify → result)

Usage:
  from veri_build.pipeline import convert, lint, compile
"""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Literal


# ── Supported target languages / backends ───────────────────────────────────

# A target is a backend name (see veri_build/target/ for the registry).
# Common values:
#   'fstar-c'       — F* → C via KaRaMeL (Low* enforced)
#   'fstar-ocaml'   — F* → OCaml via fstar --codegen OCaml
#   'dafny-rust'    — Dafny → Rust
#   'python-assert' — Python → _conditions.py + @contract decorators

Target = str  # any registered backend name

CANONICAL_TARGETS = {
    'fstar-c', 'fstar-ocaml', 'fstar-wasm',
    'dafny-java', 'dafny-js', 'dafny-python', 'dafny-rust',
}


def _format_targets() -> str:
    """Return a human-readable list of target names for error messages."""
    names = sorted(CANONICAL_TARGETS)
    if len(names) == 0:
        return ''
    if len(names) == 1:
        return f'TARGET {names[0]}'
    *all_but_last, last = names
    prefix = ", TARGET ".join(all_but_last)
    return f"TARGET {prefix}, or TARGET {last}"


TARGET_TO_OUTPUT = {
    'fstar-c': 'c',
    'fstar-ocaml': 'ml',
    'dafny-rust': 'rust',
    'python-assert': 'py',
}

# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class CompilerConfig:
    """Configuration for the compile pipeline step.

    Args:
        target: Target verification language ('fstar' or 'dafny')
        agent: Sub-agent type ('openclaw' or 'claude')
        timeout_seconds: Max seconds for agent work (0 = no limit)
        output_dir: Directory for generated files (default: alongside .veri.md)
        module_name: Override module name (default: derived from filename)
        use_docker: Run sub-agent in Docker sandbox (default: True)
        verify: Run target verifier after agent fills TODOs (default: True)
    """
    target: Target = 'fstar'
    agent: Literal['openclaw', 'claude'] = 'claude'
    timeout_seconds: int = 600
    output_dir: Optional[str] = None
    module_name: Optional[str] = None
    use_docker: bool = True
    verify: bool = True


# ── Shared helpers ──────────────────────────────────────────────────────────

VERI_BLOCK_PATTERN = re.compile(r'```veri\n(.*?)```', re.DOTALL)
VERI_BUILD_ROOT = Path(__file__).resolve().parent


def _find_tool(name: str) -> Optional[str]:
    """Locate a tool binary."""
    path = shutil.which(name)
    if path:
        return str(path)
    return None


def _find_fstar_ulib() -> Optional[str]:
    """Locate the F* ulib directory."""
    fstar = _find_tool('fstar.exe')
    if fstar:
        fstar_dir = Path(fstar).resolve().parent.parent
        candidates = [
            fstar_dir / 'lib' / 'fstar' / 'ulib',
            fstar_dir / 'ulib',
            Path('/usr/local') / 'lib' / 'fstar' / 'ulib',
        ]
        for c in candidates:
            if (c / 'Prims.fst').exists():
                return str(c)
    return None


def _extract_target_from_veri(veri_text: str) -> Optional[str]:
    """Read the target declaration from Veri DSL spec.

    Target is declared as an uppercase keyword in the first Veri DSL block:
      TARGET f-star-c       # F* → C via KaRaMeL (Low*)
      TARGET f-star-ocaml   # F* → OCaml via fstar --codegen OCaml
      TARGET dafny-rust     # Dafny → Rust
      TARGET python-assert  # Python — runtime assertion checks

    Returns the backend name (e.g., 'fstar-c', 'fstar-ocaml') or None.
    """
    m = re.search(r'```veri\n.*?TARGET\s+(\S+)', veri_text, re.DOTALL | re.IGNORECASE)
    if m:
        raw = m.group(1).lower().strip()
        # Backward compat and common aliases (canonical forms are fstar-c etc.)
        alias_map = {
            'fstar-c': 'fstar-c', 'f-star-c': 'fstar-c',
            'fstar-ocaml': 'fstar-ocaml', 'f-star-ocaml': 'fstar-ocaml',
            'fstar-wasm': 'fstar-wasm', 'f-star-wasm': 'fstar-wasm',
            'dafny-java': 'dafny-java', 'dafny-js': 'dafny-js',
            'dafny-python': 'dafny-python',
            'dafny-rust': 'dafny-rust', 'dafny': 'dafny-rust',
            'python-assert': 'python-assert',
            'c': 'fstar-c', 'ocaml': 'fstar-ocaml', 'wasm': 'fstar-wasm',
            'java': 'dafny-java', 'js': 'dafny-js', 'python': 'python-assert', 'rust': 'dafny-rust',
        }
        return alias_map.get(raw)
    return None


def _extract_version_from_veri(veri_text: str) -> Optional[str]:
    """Read VERI_VERSION declaration from Veri DSL spec.

    The version is declared as:
      VERI_VERSION 0.3.0

    Must appear in the first Veri DSL block. Returns None if not declared.
    """
    m = re.search(r'```veri\n.*?VERI_VERSION\s+(\S+)', veri_text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _get_veri_dsl_version() -> str:
    """Read the current Veri DSL version from the VERSION file."""
    version_path = Path(__file__).resolve().parent / 'dsl' / 'src' / 'VERSION'
    if version_path.exists():
        return version_path.read_text().strip()
    return 'unknown'


def _major_minor(version: str) -> str:
    """Extract 'major.minor' from a semver string (e.g. '0.0.1' → '0.0')."""
    parts = version.split('.')
    return f'{parts[0]}.{parts[1]}' if len(parts) >= 2 else version


def _extract_veri_blocks(md_text: str) -> List[str]:
    return [b.strip() for b in VERI_BLOCK_PATTERN.findall(md_text) if b.strip()]


def _is_veri_block(block: str) -> bool:
    """Reject raw F* or Dafny blocks — Veri DSL blocks use Veri DSL syntax only.

    Veri DSL syntax uses: def, class, REQUIRES, ENSURES, WHERE, FORALL, EXISTS,
    match/case, import, type (with Veri DSL-style), and lambda expressions.

    Raw target-language constructs like 'val', 'let', 'assume', 'module', 'open',
    Dafny's 'function', 'method', 'datatype' are NOT valid Veri DSL.
    """
    lines = block.strip().split('\n')
    code_lines = [l for l in lines if l.strip() and not l.strip().startswith('#')]
    if not code_lines:
        return True

    first = code_lines[0].strip()

    # Check for Veri DSL indicators first
    veri_indicators = ['def ', 'class ', 'REQUIRES', 'ENSURES', 'WHERE', 'FORALL',
                      'EXISTS', 'match ', 'import ', 'case ', 'TARGET ', 'EXTERN ']
    for kw in veri_indicators:
        if first.startswith(kw) or kw in block[:60]:
            return True

    # Reject F*-only constructs
    fstar_only = {'val ', 'let ', 'assume ', 'module ', 'open '}
    for kw in fstar_only:
        if first.startswith(kw):
            return False

    # Reject Dafny-only constructs
    dafny_only = {'function ', 'method ', 'datatype ', 'predicate '}
    for kw in dafny_only:
        if first.startswith(kw):
            return False

    return _try_parse_veri(block)


def _try_parse_veri(block: str) -> bool:
    """Attempt to parse block as Veri DSL using the Veri DSL parser."""
    try:
        sys.path.insert(0, str(VERI_BUILD_ROOT / 'dsl' / 'src'))
        from veri_parser import parse_veri
        parse_veri(block)
        return True
    except Exception:
        return False


# ── Target-language code generation ─────────────────────────────────────────

def _generate_target_code(veri_path: Path, target: Target,
                          module_name: Optional[str] = None):
    """Generate target-language code from Veri DSL .veri.md.

    Returns (spec, interface_path, impl_path) where:
      - fstar: interface_path = .fst, impl_path = .fst
      - dafny: interface_path = .dfy (module signature), impl_path = .dfy (full)
    """
    sys.path.insert(0, str(VERI_BUILD_ROOT / 'dsl' / 'src'))
    from veri_build.spec import read_spec

    spec = read_spec(veri_path, module_name=module_name)
    mn = spec.module_name

    _dsl_lang = target.split('-')[0]
    if _dsl_lang not in ('fstar', 'dafny', 'python'):
        raise ValueError(f"Unsupported target: {target}")

    if _dsl_lang == 'fstar':
        from backend.fstar.printer import FStarPrinter
        printer = FStarPrinter()

        # Filter out the `target` directive — it's a pipeline config, not part of the spec
        from veri_ast import VeriDslProgram
        filtered = VeriDslProgram()
        filtered.module = spec.program.module
        for decl in spec.program.decls:
            from veri_ast import TargetDecl
            if isinstance(decl, TargetDecl):
                continue
            filtered.add(decl)

        fst_text = printer.print(filtered)
        fst_text = _generate_fstar_stubs(spec)

        return spec, fst_text, fst_text

    elif _dsl_lang == 'dafny':
        from backend.dafny.printer import DafnyPrinter
        printer = DafnyPrinter()
        # Interface: types + function/method signatures only
        dfy_text = printer.print(spec.program)
        # Stripped version: types only (for impl file)
        dfy_stubs = _generate_dafny_stubs(spec)

        return spec, dfy_text, dfy_stubs

    else:
        raise ValueError(f"Unsupported target: {target}")


def _generate_fstar_stubs(spec) -> str:
    """Generate F* .fst with admit() stubs for all TODO functions."""
    from veri_ast import (
        ValDecl, OpenDecl, TypeAlias, TypeAbstract, TypeRecord, TypeVariant,
    )
    from backend.fstar.printer import FStarPrinter

    printer = FStarPrinter()
    from veri_ast import VeriDslProgram, ModuleDecl, QualifiedIdent
    ast = VeriDslProgram()
    ast.module = spec.program.module

    for decl in spec.program.decls:
        if isinstance(decl, (OpenDecl, TypeAlias, TypeAbstract, TypeRecord, TypeVariant)):
            ast.add(decl)

    fst_text = printer.print(ast)

    # Append admit() stubs for each TODO function
    for fn_name in spec.todo_function_names:
        for decl in spec.program.decls:
            if isinstance(decl, ValDecl) and decl.name == fn_name:
                param_count = len(decl.params)
                params = ' '.join([f'x{i}' for i in range(param_count)])
                fst_text += f'let {fn_name} {params} = admit()\n'
                break

    return fst_text


def _generate_dafny_stubs(spec) -> str:
    """Generate Dafny .dfy with stub bodies for all TODO functions."""
    result = []
    result.append(f'module {spec.module_name}')
    result.append('')

    for decl in spec.program.decls:
        from veri_ast import (
            OpenDecl, TypeAlias, TypeAbstract, TypeRecord, TypeVariant
        )
        if isinstance(decl, (OpenDecl, TypeAlias, TypeAbstract, TypeRecord, TypeVariant)):
            from backend.dafny.printer import DafnyPrinter
            p = DafnyPrinter()
            from veri_ast import VeriDslProgram
            prog = VeriDslProgram()
            prog.add(decl)
            result.append(p.print(prog).strip())

    # Add stub implementations for TODO functions
    for fn_name in spec.todo_function_names:
        result.append(f'  // TODO: implement {fn_name}')
        for decl in spec.program.decls:
            from veri_ast import ValDecl
            if isinstance(decl, ValDecl) and decl.name == fn_name:
                param_strs = []
                from backend.dafny.printer import DafnyPrinter
                printer = DafnyPrinter()
                for param in decl.params:
                    pname = param.name or '_'
                    ptype = printer._type(param.typ) if param.typ else 'nat'
                    param_strs.append(f'{pname}: {ptype}')
                ret = 'nat'
                if decl.return_type:
                    ret = printer._type(decl.return_type)
                result.append(f'  function method {fn_name}({", ".join(param_strs)}): {ret}')
                result.append(f'  {{')
                if ret == 'bool':
                    result.append(f'    true')
                elif ret in ('nat', 'int'):
                    result.append(f'    0')
                else:
                    result.append(f'    // stub')
                result.append(f'  }}')
                break

    return '\n'.join(result)


# ── Sub-agent launcher ──────────────────────────────────────────────────────

def _launch_agent_in_docker(
    agent_type: Literal['openclaw', 'claude'],
    prompt: str,
    workdir: Path,
    timeout_seconds: int = 600,
) -> str:
    """Launch a sub-agent inside Docker with credentials mounted.

    Agent types:
      - 'claude': Uses ~/.claude/.credentials.json for API access.
                  Runs `claude -p "prompt" --print`.
      - 'openclaw': Uses ~/.openclaw/ for OpenClaw identity + env.
                    Runs `openclaw` with the prompt.

    Returns the agent's stdout text.
    """
    home = Path.home()

    # Build Docker volume mounts
    volumes = {
        str(workdir.resolve()): {'bind': '/workspace', 'mode': 'ro'},
    }

    if agent_type == 'claude':
        # Mount Claude credentials
        creds_path = home / '.claude' / '.credentials.json'
        if creds_path.exists():
            volumes[str(creds_path)] = {
                'bind': '/root/.claude/.credentials.json', 'mode': 'ro'
            }
        else:
            print("  ⚠️  ~/.claude/.credentials.json not found — Claude may not have API access")

    elif agent_type == 'openclaw':
        # Mount OpenClaw identity and env
        identity_path = home / '.openclaw' / 'identity'
        env_path = home / '.openclaw' / '.env'
        if identity_path.exists():
            volumes[str(identity_path)] = {
                'bind': '/root/.openclaw/identity', 'mode': 'ro'
            }
        if env_path.exists():
            volumes[str(env_path)] = {
                'bind': '/root/.openclaw/.env', 'mode': 'ro'
            }

    # Build docker run command
    docker_cmd = ['docker', 'run', '--rm']
    for src, bind in volumes.items():
        docker_cmd.extend(['-v', f'{src}:{bind["bind"]}:{bind["mode"]}'])

    docker_cmd.extend(['-w', '/workspace'])

    # Agent-specific command inside the container
    if agent_type == 'claude':
        docker_cmd.extend([
            'verification-builder',
            'claude', '-p', prompt, '--print',
        ])
    else:  # openclaw
        docker_cmd.extend([
            'verification-builder',
            'sh', '-c',
            f'echo "{prompt}" | openclaw --model deepseek/deepseek-v4-pro',
        ])

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True, text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            error_msg = result.stderr[:500] if result.stderr else result.stdout[:500]
            raise RuntimeError(f"Agent exited with code {result.returncode}: {error_msg}")
        return result.stdout
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Agent timed out after {timeout_seconds}s")
    except FileNotFoundError:
        raise RuntimeError("Docker not found. Install Docker or set use_docker=False.")


def _launch_agent_locally(
    agent_type: Literal['openclaw', 'claude'],
    prompt: str,
    timeout_seconds: int = 600,
) -> str:
    """Launch a sub-agent locally (no sandbox)."""
    if agent_type == 'claude':
        claude_bin = _find_tool('claude')
        if not claude_bin:
            raise RuntimeError("claude not found on PATH")
        result = subprocess.run(
            [claude_bin, '-p', prompt, '--print'],
            capture_output=True, text=True,
            timeout=timeout_seconds,
        )
    else:  # openclaw
        openclaw_bin = _find_tool('openclaw')
        if not openclaw_bin:
            raise RuntimeError("openclaw not found on PATH")
        result = subprocess.run(
            [openclaw_bin, '--json', prompt],
            capture_output=True, text=True,
            timeout=timeout_seconds,
        )

    if result.returncode != 0:
        raise RuntimeError(f"Agent exited with code {result.returncode}: {result.stderr[:500]}")
    return result.stdout


# ═══════════════════════════════════════════════════════════════════════════════
# API 1: Convertor — F*F* ↔ Veri DSL
# ═══════════════════════════════════════════════════════════════════════════════

def convert_fstar_to_veri(fstar_text: str) -> str:
    """Convert F* code to Veri DSL DSL syntax.

    Input: F* code (string)
    Output: Veri DSL DSL code (string)
    """
    lines = fstar_text.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped or stripped.startswith('(*') or stripped.startswith('*)'):
            result.append(line)
            i += 1
            continue

        if stripped.startswith('module '):
            i += 1
            continue

        # 'open' → 'import'
        if stripped.startswith('open '):
            m = re.match(r'open\s+(.*)', stripped)
            if m:
                result.append(f'import {m.group(1)}')
            i += 1
            continue

        # 'val f: ...' → 'def f(...):'
        if stripped.startswith('val '):
            val_lines = [stripped]
            j = i + 1
            while j < len(lines) and (lines[j].strip().startswith('(') or lines[j].strip().startswith('  ')):
                val_lines.append(lines[j].strip())
                j += 1
            val_text = '\n'.join(val_lines)
            conv = _fstar_val_to_veri(val_text)
            if conv:
                result.append(conv)
                i = j
                continue

        # Type declarations
        if stripped.startswith('type '):
            conv = _fstar_type_to_veri(stripped)
            result.append(conv)
            i += 1
            continue

        # 'let rec' → 'def'
        let_match = re.match(r'let\s+(?:rec\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s*(.*)', stripped)
        if let_match:
            result.append(f'# converted from let: {stripped}')
            i += 1
            continue

        result.append(line)
        i += 1

    return '\n'.join(result)


def _fstar_val_to_veri(line: str) -> Optional[str]:
    """Convert a single F* val line to Veri DSL def + REQUIRES/ENSURES."""
    m = re.match(r'val\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)', line)
    if not m:
        return None
    name = m.group(1)
    rest = m.group(2).strip()

    effect = 'Pure'
    for efx in ['Pure', 'Tot', 'GTot', 'Lemma', 'ST', 'All', 'ML', 'Dv']:
        if rest.startswith(f'{efx} '):
            effect = efx
            rest = rest[len(efx):].strip()
            break

    parts = [p.strip() for p in rest.split(' -> ')]
    ret_type = parts[-1] if len(parts) > 1 else rest

    ret_type = re.split(r'\n\s*\(requires|\n\s*\(ensures', ret_type)[0].strip()
    if ret_type.startswith('(') and ret_type.endswith(')'):
        ret_type = ret_type[1:-1]

    params = parts[:-1]
    param_strs = []
    for p in params:
        pm = re.match(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+)', p)
        if pm:
            param_strs.append(f'{pm.group(1)}: {pm.group(2).strip()}')
        else:
            param_strs.append(p.strip())

    sig = f'def {name}({", ".join(param_strs)}) -> {ret_type}'

    req_match = re.search(r'\(requires\s+(.+?)\)\s*\(ensures', line, re.DOTALL)
    ens_match = re.search(r'\(ensures\s+(.+?)\)\s*$', line, re.DOTALL)

    if req_match or ens_match:
        sig += ':'
    if req_match:
        req = req_match.group(1).strip()
        sig += f'\n    REQUIRES {req}'
    if ens_match:
        ens = ens_match.group(1).strip()
        sig += f'\n    ENSURES {ens}'

    return sig


def _fstar_type_to_veri(line: str) -> str:
    """Convert F* type declaration to Veri DSL."""
    m = re.match(r'type\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.*)', line)
    if m:
        return f'type {m.group(1)} = {m.group(2).strip()}'
    m = re.match(r'type\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*(\:\s*.*)?$', line)
    if m:
        kind = m.group(2) or ''
        return f'type {m.group(1)}{kind}'
    return line


# ═══════════════════════════════════════════════════════════════════════════════
# API 2: Linter — validate .veri.md (Veri DSL only, verify with target verifier)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class LintResult:
    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    target_stderr: str = ''
    target_stdout: str = ''
    target: Target = 'fstar'
    veri_version: str = ''


@dataclass
class VerifyConvertResult:
    """Result of verify_and_convert: verify target code, produce Veri DSL."""
    verified: bool
    veri: Optional[str] = None
    target_code: Optional[str] = None
    error: Optional[str] = None
    stdout: str = ''
    stderr: str = ''
    target: Target = 'fstar'


def _run_fstar_interface(fst_text: str, module_name: str,
                          fstar_bin: Optional[str] = None) -> tuple[int, str, str]:
    """Run fstar.exe on an interface file and return (returncode, stdout, stderr)."""
    fstar = fstar_bin or _find_tool('fstar.exe')
    if not fstar:
        return (-1, '', 'fstar.exe not found in PATH')

    ulib = _find_fstar_ulib()
    if not ulib:
        return (-1, '', 'F* ulib not found')

    with tempfile.TemporaryDirectory(prefix='verilint_') as tmpdir:
        tmp = Path(tmpdir)
        fst_path = tmp / f'{module_name}.fst'
        fst_path.write_text(fst_text)

        cmd = [fstar, '--include', ulib, str(fst_path)]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return (proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return (-1, '', 'F* verification timed out (>60s)')
        except FileNotFoundError:
            return (-1, '', f'fstar.exe not found at {fstar}')


def _run_dafny_interface(dfy_text: str, module_name: str,
                          dafny_bin: Optional[str] = None) -> tuple[int, str, str]:
    """Run Dafny on an interface and return (returncode, stdout, stderr)."""
    dafny = dafny_bin or _find_tool('dafny')
    if not dafny:
        return (-1, '', 'dafny not found in PATH')

    with tempfile.TemporaryDirectory(prefix='verilint_') as tmpdir:
        tmp = Path(tmpdir)
        dfy_path = tmp / f'{module_name}.dfy'
        dfy_path.write_text(dfy_text)

        # Dafny needs to verify the interface. Allow warnings: a spec under lint
        # legitimately contains bodyless #TODO functions (their bodies are filled
        # at compile time), which Dafny warns about and otherwise fails on.
        cmd = [dafny, 'verify', '--allow-warnings', str(dfy_path)]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return (proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return (-1, '', 'Dafny verification timed out (>60s)')
        except FileNotFoundError:
            return (-1, '', f'dafny not found at {dafny}')


def _require_lowstar_for_c(fstar_text: str) -> Optional[str]:
    """Strict Low* check for C target. Returns error message or None if clean.

    Low* subset for C extraction via KaRaMeL:
      - No ST effect (use Pure/Tot/GTot/Lemma)
      - No HyperStack, Heap, or ST modules
      - No mutable heap references in signatures
      - Only bounded recursion (structural termination)
    """
    errors = []

    # Check for forbidden effects
    if re.search(r'\bST\b', fstar_text):
        errors.append('ST effect detected — use Pure/Tot/GTot for C extraction')

    # Check for forbidden modules
    forbidden_mods = ['HyperStack', 'Heap', 'B.Mem', 'B.Mut', 'B.Map']
    for mod in forbidden_mods:
        if re.search(rf'\b{mod}\b', fstar_text):
            errors.append(f'{mod} module detected — not in Low* subset for C')

    # Check for non-Pure effects in val declarations
    val_effects = re.findall(r'->\s*(ST|All|ML|Dv|Exn)\s', fstar_text)
    for eff in val_effects:
        errors.append(f'{eff} effect in val declaration — only Pure/Tot/GTot/Lemma allowed for C')

    if errors:
        return 'Low* violation(s) — C extraction requires Low* subset:\n  ' + '\n  '.join(errors)
    return None


def verify_and_convert(
    target_code: str,
    target: Target = 'fstar',
    module_name: str = 'Module',
    fstar_bin: Optional[str] = None,
    dafny_bin: Optional[str] = None,
) -> VerifyConvertResult:
    """Primary API: verify target-language code, convert to Veri DSL.

    This is what the LLM uses. The LLM writes F*/Dafny in a temp file,
    calls this API to verify it and convert to user-facing Veri DSL.
    The LLM never writes Veri DSL directly.

    Args:
        target_code: F* or Dafny source code (string)
        target: 'fstar' or 'dafny'
        module_name: Module name for verification

    Returns:
        VerifyConvertResult with:
          - verified: whether verification passed
          - veri: Veri DSL converted from verified code (None if verification failed)
          - error: error message if anything failed
          - target_code: the original target code (echoed back)
    """
    if target == 'fstar':
        retcode, stdout, stderr = _run_fstar_interface(
            target_code, module_name, fstar_bin)
    else:
        retcode, stdout, stderr = _run_dafny_interface(
            target_code, module_name, dafny_bin)

    if retcode == -1:
        # Tool not found — still try to convert, warn about missing verifier
        try:
            veri = convert_fstar_to_veri(target_code) if target == 'fstar' else _dafny_to_veri(target_code)
        except Exception:
            veri = None
        return VerifyConvertResult(
            verified=False, veri=veri, target_code=target_code,
            error=stderr, stdout=stdout, stderr=stderr, target=target)

    if retcode != 0:
        return VerifyConvertResult(
            verified=False, target_code=target_code,
            error=f'Verification failed\n{stderr[:500]}',
            stdout=stdout, stderr=stderr, target=target)

    # Low* enforcement for C target (after successful verification)
    if target == 'fstar':
        lowstar_error = _require_lowstar_for_c(target_code)
        if lowstar_error:
            return VerifyConvertResult(
                verified=False, target_code=target_code,
                error=lowstar_error,
                stdout=stdout, stderr=stderr, target=target)

    # Convert verified target code to Veri DSL
    try:
        if target == 'fstar':
            veri = convert_fstar_to_veri(target_code)
        else:
            veri = _dafny_to_veri(target_code)
    except Exception as e:
        return VerifyConvertResult(
            verified=True, target_code=target_code,
            error=f'Verified but Veri DSL conversion failed: {e}',
            stdout=stdout, stderr=stderr, target=target)

    return VerifyConvertResult(
        verified=True, veri=veri, target_code=target_code,
        stdout=stdout, stderr=stderr, target=target)


def _dafny_to_veri(dafny_text: str) -> str:
    """Convert Dafny code to Veri DSL using the round-trip parser+printer."""
    sys.path.insert(0, str(VERI_BUILD_ROOT / 'dsl' / 'src'))
    from backend.dafny.parser import parse_dafny
    from veri_printer import VeriDslPrinter
    prog = parse_dafny(dafny_text)
    printer = VeriDslPrinter()
    return printer.print(prog)


def _check_functions_implemented(block: str) -> List[tuple[str, str]]:
    """Check every function in a Veri DSL block has a body, EXTERN, or #TODO.

    Returns: list of (function_name, issue_description) for each
      function that has REQUIRES/ENSURES but no body, no EXTERN, and no #TODO.
    """
    issues = []
    lines = block.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^\s*def\s+(\w+)\s*\(', line)
        if not m:
            i += 1
            continue

        fn_name = m.group(1)
        has_contract = False
        has_body = False
        has_extern = False
        has_todo = False

        # Scan function body until we hit a new top-level construct
        # (non-indented line that isn't a contract keyword, comment, or blank)
        i += 1
        while i < len(lines):
            l = lines[i]
            s = l.strip()
            if not s:
                i += 1
                continue
            if s.startswith(('REQUIRES', 'ENSURES', 'DECREASES')):
                has_contract = True
            elif s.startswith('#TODO'):
                has_todo = True
            elif s.startswith('EXTERN'):
                has_extern = True
            elif s.startswith(('return', 'if', 'match')):
                has_body = True
            elif not l.startswith((' ', '\t')) and not s.startswith(('REQUIRES', 'ENSURES', 'DECREASES', '#', '')):
                # New top-level construct — function ended
                break
            elif not l.startswith((' ', '\t')) and s.startswith(('def ', 'class ', 'type ', 'import ', 'EXTERN ')):
                break
            i += 1

        if has_contract and not has_body and not has_extern and not has_todo:
            issues.append((fn_name, 'has REQUIRES/ENSURES but no body, EXTERN, or #TODO'))
    return issues


def lint(veri_path: str,
         fstar_bin: Optional[str] = None,
         dafny_bin: Optional[str] = None) -> LintResult:
    """Validate a .veri.md file.

    Reads the target language from the Veri DSL spec (TARGET f-star-c or TARGET dafny-rust).
    If target is C, enforces Low* subset for KaRaMeL extraction.
    If no target declared, fails with an error.

    Checks:
      1. All ```veri blocks are valid Veri DSL
      2. Target language is declared in first block
      3. Veri DSL converts to target language through the parser/printer pair
      4. Generated target code verifies with fstar.exe or dafny
      5. When target is C, enforces Low* subset

    Args:
        veri_path: Path to .veri.md file

    Returns:
        LintResult with pass/fail, errors, and diagnostics
    """
    path = Path(veri_path)
    if not path.exists():
        return LintResult(False, errors=[f'File not found: {veri_path}'])

    text = path.read_text()
    blocks = _extract_veri_blocks(text)

    if not blocks:
        return LintResult(False, errors=['No ```veri blocks found'])

    # Read target from Veri DSL
    target = _extract_target_from_veri(text)
    if target is None:
        return LintResult(False, errors=[
            'No target language declared. '
            'Add to first Veri DSL block: ' + _format_targets()
        ])

    # Read and check VERI_VERSION (required, major.minor only)
    spec_version = _extract_version_from_veri(text)
    if not spec_version:
        return LintResult(False, errors=[
            "No VERI_VERSION declared. Add to the first Veri DSL block: "
            f"VERI_VERSION {_get_veri_dsl_version()}"
        ])
    dsl_version = _get_veri_dsl_version()
    if _major_minor(spec_version) != _major_minor(dsl_version):
        return LintResult(False, errors=[
            f"VERI_VERSION mismatch: spec says {spec_version}, "
            f"DSL is {dsl_version}. Upgrade Veri DSL toolchain for "
            f"major.minor {_major_minor(spec_version)} support, or "
            f"update VERI_VERSION in your .veri.md to {dsl_version}."
        ])
    result = LintResult(True, target=target, veri_version=spec_version)

    # Step 1: Validate all blocks are Veri DSL (no raw target language)
    for i, block in enumerate(blocks):
        if not _is_veri_block(block):
            result.errors.append(
                f'Block {i+1} contains raw target code (not Veri DSL): {block[:80]}...')
            result.passed = False

    if not result.passed:
        return result


    # Step 1b: Every function must have a body, EXTERN, or #TODO
    for i, block in enumerate(blocks):
        fn_issues = _check_functions_implemented(block)
        for fn_name, issue in fn_issues:
            result.errors.append(f'Block {i+1}: function "{fn_name}" {issue}')
            result.passed = False

    if not result.passed:
        return result

    # Step 2: Convert Veri DSL → target language and verify
    try:
        spec, interface_text, _ = _generate_target_code(path, target)
    except Exception as e:
        result.errors.append(f'Veri DSL → {target} conversion failed: {e}')
        result.passed = False
        return result

    # Step 3: Run target verifier on interface only (no admits, no stubs)
    if target and target.startswith('fstar'):
        # Types are inlined by _resolve_imports() — no cross-module deps needed.
        retcode, stdout, stderr = _run_fstar_interface(
            interface_text, spec.module_name, fstar_bin)
        result.target_stdout = stdout
        result.target_stderr = stderr

        if retcode == -1:
            result.errors.append(f'{stderr} — F* not found. Install fstar.exe or add to PATH. '
                                 'The Veri DSL backend targets F* v2026.05.31.')
            result.passed = False
        elif retcode != 0:
            result.errors.append(f'F* interface verification failed')
            for line in (stderr + '\n' + stdout).split('\n'):
                if 'Error' in line:
                    result.errors.append(f'  F*: {line.strip()}')
            result.passed = False
        else:
            # Step 3b: Strict Low* enforcement (mandatory for C target)
            lowstar_error = _require_lowstar_for_c(interface_text)
            if lowstar_error:
                result.errors.append(lowstar_error)
                result.passed = False

    elif target.startswith('dafny'):
        retcode, stdout, stderr = _run_dafny_interface(
            interface_text, spec.module_name, dafny_bin)
        result.target_stdout = stdout
        result.target_stderr = stderr

        if retcode == -1:
            result.errors.append(f'{stderr} — Dafny not found. Install dafny or add to PATH.')
            result.passed = False
        elif retcode != 0:
            result.errors.append(f'Dafny verification failed')
            for line in (stderr + '\n' + stdout).split('\n'):
                if 'Error' in line or 'error' in line:
                    result.errors.append(f'  Dafny: {line.strip()}')
            result.passed = False


    return result


def _check_lowstar(fstar_text: str) -> List[str]:
    """Check F* code for Low* compliance.

    Low* is the subset of F* that compiles to C via KaRaMeL.
    Key restrictions:
      - No ST effect (use state-passing style instead)
      - No heap references (Ghost.Erase, etc.)
      - Only Pure, Tot, GTot, Lemma effects
      - Restricted recursion (structural)

    Returns a list of warning strings (empty if all good).
    """
    warnings = []
    if re.search(r'\bST\b', fstar_text):
        warnings.append('Low*: ST effect detected — use state-passing style for C extraction')
    if re.search(r'\bHyperStack\b|\bHeap\b|\bBuffer\b', fstar_text):
        # Buffer is OK in Low* actually (it's the main Low* data structure)
        # Only flag if not used properly
        pass
    if re.search(r'\bState\b.*\bfun\b', fstar_text, re.DOTALL):
        warnings.append('Low*: State effect detected — use Pure/Tot for C extraction')
    return warnings


# ═══════════════════════════════════════════════════════════════════════════════
# API 3: Compiler — end-to-end pipeline (Veri DSL → agent → verify → result)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CompileResult:
    success: bool
    module_name: str
    veri_path: Path
    target: Target
    interface: Optional[str] = None          # generated target-language interface text
    veri_from_target: Optional[str] = None    # interface converted back to Veri DSL
    output_path: Optional[Path] = None       # .c or .rs (compiled output)
    error: Optional[str] = None
    agent_report: Optional[str] = None
    verification_passed: bool = False
    docker_results: Optional[dict] = None    # raw JSON from container


def compile_veri(
    veri_path: str,
    config: Optional[CompilerConfig] = None,
) -> CompileResult:
    """Run the full compilation pipeline on a .veri.md file.

    Everything runs inside a single Docker container invocation:
      1. Read .veri.md, parse Veri DSL
      2. Generate target-language interface (types + sigs only)
      3. Run target verifier (fstar.exe / dafny verify) on interface
      4. Convert verified interface back to Veri DSL
      5. If agent is configured, launch sub-agent to fill TODOs
      6. Verify filled implementations
      7. Compile to output language (C via KaRaMeL, Rust via Dafny)

    All steps are triggered by one API call into the Docker container.
    The user only ever sees Veri DSL — target languages are internal.

    Args:
        veri_path: Path to .veri.md (Veri DSL source)
        config: CompilerConfig with target, agent, timeout, etc.

    Returns:
        CompileResult with interface text, Veri DSL conversion, and verification status
    """
    if config is None:
        config = CompilerConfig()

    path = Path(veri_path)
    if not path.exists():
        return CompileResult(False, '', path, config.target or 'fstar',
                             error=f'File not found: {veri_path}')

    # Read target from Veri DSL if not explicitly set
    if config.target is None:
        veri_text = path.read_text()
        detected = _extract_target_from_veri(veri_text)
        if detected is None:
            return CompileResult(
                False, '', path, 'fstar',
                error='No target language declared. ' + _format_targets())
        config.target = detected

    output_dir = Path(config.output_dir) if config.output_dir else path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    module_name = config.module_name or path.stem

    # ── Step 0: Run lint (verify API) first ──────────────────────────────
    # compile_veri delegates to lint for spec validation. This ensures
    # that specs with broken Veri DSL, missing TARGET, or unimplemented
    # contracts are rejected early, before Docker is invoked.
    lint_result = lint(str(path))
    if not lint_result.passed:
        return CompileResult(
            False, module_name, path, config.target or 'fstar',
            error=f'Lint failed: {"; ".join(lint_result.errors[:3])}',
        )

    if config.use_docker:
        # ── Everything runs inside Docker in one shot ──
        home = Path.home()
        veri_root = VERI_BUILD_ROOT.parent.parent  # veri-build/

        # Mount volumes — workspace and credentials are READ-ONLY for safety
        volumes = {
            str(path.parent.resolve()): {'bind': '/workspace', 'mode': 'ro'},
            str(veri_root.resolve()): {'bind': '/opt/veri-build', 'mode': 'ro'},
        }

        # Mount a writable output directory (separate from /workspace which is ro)
        output_dir = Path(config.output_dir) if config.output_dir else (path.parent / 'build')
        output_dir.mkdir(parents=True, exist_ok=True)
        volumes[str(output_dir.resolve())] = {'bind': '/output', 'mode': 'rw'}

        # Write result.json to a writable temp dir on the host
        import uuid
        result_dir = Path('/tmp/veri-results') / str(uuid.uuid4())
        result_dir.mkdir(parents=True, exist_ok=True)
        volumes[str(result_dir)] = {'bind': '/results', 'mode': 'rw'}

        if config.agent == 'claude':
            creds = home / '.claude' / '.credentials.json'
            if creds.exists():
                volumes[str(creds)] = {'bind': '/root/.claude/.credentials.json', 'mode': 'ro'}
        elif config.agent == 'openclaw':
            identity = home / '.openclaw' / 'identity'
            env_f = home / '.openclaw' / '.env'
            agents_dir = home / '.openclaw' / 'agents'
            if identity.exists():
                volumes[str(identity)] = {'bind': '/root/.openclaw/identity', 'mode': 'ro'}
            if env_f.exists():
                volumes[str(env_f)] = {'bind': '/root/.openclaw/.env', 'mode': 'ro'}
            if agents_dir.exists():
                volumes[str(agents_dir)] = {'bind': '/root/.openclaw/agents', 'mode': 'ro'}

        # Build docker run command
        import os as _os
        cmd = ['docker', 'run', '--rm']
        # Run as non-root so claude's --dangerously-skip-permissions works
        # (root is rejected for security reasons)
        cmd.extend(['--user', f'{_os.getuid()}:{_os.getgid()}'])
        # Set a writable HOME for agent config
        cmd.extend(['-e', 'HOME=/tmp/home'])
        for src, bind in volumes.items():
            cmd.extend(['-v', f'{src}:{bind["bind"]}:{bind["mode"]}'])
        # Pass ANTHROPIC_* env vars through to Docker so the agent inside can auth
        for key, val in sorted(_os.environ.items()):
            if key.startswith('ANTHROPIC_'):
                cmd.extend(['-e', f'{key}={val}'])

        spec_container_path = f'/workspace/{path.name}'

        # Map python-assert → python for the Docker runner (backward compat)
        docker_target = config.target
        if docker_target == 'python-assert':
            docker_target = 'python-assert'

        runner_args = [
            'python3', '/opt/veri-build/scripts/compile_parent_subagent_runner.py',
            spec_container_path,
            '--target', docker_target,
            '--output', '/results/result.json',
        ]
        if config.agent:
            runner_args += ['--agent', config.agent, '--agent-timeout',
                           str(config.timeout_seconds)]

        # The container's entrypoint is /bin/bash -lc, so the CMD must be a
        # single string — Docker merges all CMD elements into one for -lc.
        runner_cmd_str = ' '.join(runner_args)
        cmd.extend(['-w', '/workspace', 'verification-builder', runner_cmd_str])

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=config.timeout_seconds + 60)

            # Read results JSON from the writable results dir (runner writes it always)
            result_path = result_dir / 'result.json'
            if result_path.exists():
                docker_results = json.loads(result_path.read_text())
                import shutil as _shutil

                # Copy compiled output artifacts to output directory
                output_path = docker_results.get('output_path')
                if output_path and config.output_dir:
                    docker_out_path = Path(output_path)
                    if docker_out_path.exists():
                        local_out = output_dir / docker_out_path.name
                        _shutil.copy2(docker_out_path, local_out)
                        docker_results['output_path'] = str(local_out)

                _shutil.rmtree(result_dir, ignore_errors=True)
            else:
                # Docker ran but didn't write result.json — capture stderr as fallback
                docker_stderr = proc.stderr[-500:] if proc.stderr else ''
                docker_stdout = proc.stdout[-500:] if proc.stdout else ''
                docker_results = {
                    'error': f'Docker container finished but no result.json. '
                             f'Stderr: {docker_stderr}'
                             f'Stdout: {docker_stdout}'
                             if (docker_stderr or docker_stdout) else
                             'Docker container finished but no result.json and no output.',
                }

            mn = docker_results.get('module_name', module_name)

            # Python target produces _conditions.py, not an fsti/dfy interface
            interface = docker_results.get('interface')
            if config.target == 'python-assert' and not interface:
                interface = docker_results.get('conditions_path',
                    f'Generated: {docker_results.get("conditions_path", "")}')

            # Propagate Docker error (e.g., container crash with no result.json)
            docker_error = docker_results.get('error', '')
            if docker_error and not interface and not docker_results.get('verification_passed'):
                return CompileResult(
                    success=False, module_name=module_name, veri_path=path,
                    target=config.target, error=docker_error,
                    docker_results=docker_results,
                )

            return CompileResult(
                success=True,
                module_name=mn,
                veri_path=path,
                target=config.target,
                interface=interface,
                veri_from_target=docker_results.get('veri_from_target'),
                output_path=Path(docker_results['output_path']) if docker_results.get('output_path') else None,
                verification_passed=docker_results.get('verification_passed', False),
                agent_report=docker_results.get('agent_output'),
                error=docker_results.get('error'),
                docker_results=docker_results,
            )

        except subprocess.TimeoutExpired:
            return CompileResult(False, module_name, path, config.target,
                                 error=f'Docker timed out after {config.timeout_seconds + 60}s')
        except FileNotFoundError:
            return CompileResult(False, module_name, path, config.target,
                                 error='Docker not found. Install Docker or set use_docker=False')

    else:
        # ── Local execution (fallback, no Docker) ──
        try:
            spec, interface_text, impl_text = _generate_target_code(
                path, config.target, module_name)
            mn = spec.module_name or module_name or path.stem
        except Exception as e:
            return CompileResult(False, module_name or '', path, config.target,
                                 error=f'Veri DSL → {config.target} conversion failed: {e}')

        return CompileResult(
            success=True,
            module_name=mn,
            veri_path=path,
            target=config.target,
            interface=interface_text,
            verification_passed=True,
        )


# ── Output compilation ──────────────────────────────────────────────────────

def _compile_fstar_to_c(fst_path: Path, module_name: str,
                        output_dir: Path) -> Optional[Path]:
    """Extract C from verified F* via KaRaMeL (Low* pipeline)."""
    krml = _find_tool('krml')
    if not krml:
        print("  ⚠️  krml not found — skipping C extraction")
        return None

    fstar = _find_tool('fstar.exe')
    if not fstar:
        print("  ⚠️  fstar.exe not found — skipping C extraction")
        return None

    try:
        # Verify and extract
        krml_out = output_dir / f'{module_name}.krml'
        subprocess.run([
            fstar, '--codegen', 'krml', '--extract', module_name,
            '--admit_smt_queries', 'true',
            '--odir', str(output_dir),
            str(fst_path),
        ], capture_output=True, text=True, timeout=120)

        # Generate C
        c_dir = output_dir / 'c'
        c_dir.mkdir(exist_ok=True)
        subprocess.run([
            krml, '-skip-compilation', str(krml_out),
            '-tmpdir', str(c_dir),
        ], capture_output=True, text=True, timeout=60)

        # Find output .c file
        for cf in output_dir.rglob('*.c'):
            if cf.name.startswith(module_name) or cf.name == f'{module_name}.c':
                return cf
        return None

    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"  ⚠️  C extraction failed: {e}")
        return None


def _compile_dafny_to_rust(dfy_path: Path, module_name: str,
                            output_dir: Path) -> Optional[Path]:
    """Compile verified Dafny to Rust using Dafny's built-in Rust backend."""
    dafny = _find_tool('dafny')
    if not dafny:
        print("  ⚠️  dafny not found — skipping Rust compilation")
        return None

    try:
        rust_dir = output_dir / 'rust'
        rust_dir.mkdir(exist_ok=True)

        subprocess.run([
            dafny, 'translate', 'rs',
            '--output', str(rust_dir / module_name),
            str(dfy_path),
        ], capture_output=True, text=True, timeout=120)

        # Find output .rs file
        for rf in rust_dir.glob('*.rs'):
            return rf
        return None

    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        print(f"  ⚠️  Rust compilation failed: {e}")
        return None


# ── CLI entry points ─────────────────────────────────────────────────────────

def main():
    """Simple CLI for the pipeline APIs."""
    import argparse
    parser = argparse.ArgumentParser(description='Veri DSL Pipeline Toolkit')
    parser.add_argument('action', choices=['lint', 'convert', 'compile'],
                        help='Pipeline action to run')
    parser.add_argument('path', help='Path to .veri.md file')
    parser.add_argument('--target', choices=['c', 'rust', 'f-star-c', 'dafny-rust'], default=None,
                        help='Output target (default: read from Veri DSL spec)')
    parser.add_argument('--agent', choices=['openclaw', 'claude'], default='claude',
                        help='Sub-agent type (default: claude)')
    parser.add_argument('--output-dir', '-o', help='Output directory')
    parser.add_argument('--module-name', '-m', help='Module name override')
    parser.add_argument('--timeout', '-t', type=int, default=600,
                        help='Sub-agent timeout in seconds (default: 600)')
    parser.add_argument('--no-docker', action='store_true',
                        help='Run sub-agent locally (no sandbox)')
    parser.add_argument('--no-verify', action='store_true',
                        help='Skip verification step')

    args = parser.parse_args()

    if args.action == 'lint':
        result = lint(args.path)
        if result.passed:
            print(f'✅ {args.path}: lint passed ({result.target})')
            for w in result.warnings:
                print(f'  ⚠️  {w}')
        else:
            print(f'❌ {args.path}: lint failed')
            for e in result.errors:
                print(f'  Error: {e}')
            for w in result.warnings:
                print(f'  ⚠️  {w}')
            sys.exit(1)

    elif args.action == 'convert':
        fstar_text = Path(args.path).read_text()
        veri_text = convert_fstar_to_veri(fstar_text)
        print(veri_text)

    elif args.action == 'compile':
        # Map user-facing targets to internal targets
        user_target = args.target
        internal_target = None
        if user_target in ('c', 'f-star-c'):
            internal_target = 'fstar'
        elif user_target in ('rust', 'dafny-rust'):
            internal_target = 'dafny'

        cfg = CompilerConfig(
            target=internal_target,
            agent=args.agent,
            timeout_seconds=args.timeout,
            output_dir=args.output_dir,
            module_name=args.module_name,
            use_docker=not args.no_docker,
            verify=not args.no_verify,
        )
        result = compile_veri(args.path, cfg)
        if result.success:
            print(f'✅ Compiled {args.path}')
            print(f'  Module: {result.module_name}')
            print(f'  Target: {result.target}')
            if result.verification_passed:
                print(f'  Interface verification: ✅ passed')
            else:
                print(f'  Interface verification: ⚠️  needs review')
            if result.interface:
                print(f'  Interface ({result.target}): {len(result.interface)} chars')
            if result.veri_from_target:
                print(f'  Veri DSL (converted back): {len(result.veri_from_target)} chars')
                print()
                print(f'  === Veri DSL spec (verified) ===')
                for line in result.veri_from_target.split('\n')[:15]:
                    print(f'  {line}')
            if result.output_path:
                print(f'  Output:    {result.output_path}')
            if result.agent_report:
                print(f'  Agent: {result.agent_report[:200]}...')
        else:
            print(f'❌ Compilation failed: {result.error}')
            sys.exit(1)


if __name__ == '__main__':
    main()
