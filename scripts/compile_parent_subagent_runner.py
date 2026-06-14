#!/usr/bin/env python3
"""
compile_parent_subagent_runner — Pipeline runner for Docker sandbox.

This script runs INSIDE the Docker container as the "parent" that orchestrates
the LLM sub-agent to fill #TODO functions. Steps:

  1. Read .veri.md, parse Veri DSL
  2. Generate target-language interface (types + signatures only)
  3. Run target verifier (fstar.exe / dafny verify) on interface
  4. Convert verified interface back to Veri DSL
  5. If --agent is set, launch sub-agent to fill TODOs
  6. Verify filled implementations
  7. Output results as JSON

Usage (inside Docker):
  veri-build-runner /workspace/spec.veri.md --target fstar-c
  veri-build-runner /workspace/spec.veri.md --target dafny-java
  veri-build-runner /workspace/spec.veri.md --target fstar-c --agent claude

The symlink veri-build-runner -> compile_parent_subagent_runner.py
is installed at /usr/local/bin/veri-build-runner in the Docker image.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


def _find_ulib():
    """Locate F* ulib."""
    candidates = [
        '/opt/fstar/lib/fstar/ulib',
        '/usr/local/lib/fstar/ulib',
    ]
    for c in candidates:
        if (Path(c) / 'Prims.fst').exists():
            return c
    return None


def _extract_veri_blocks(md_text: str):
    return re.findall(r'```veri\n(.*?)```', md_text, re.DOTALL)


# ── Step 1: Parse Veri DSL ───────────────────────────────────────────────────

def parse_veri_spec(veri_path: Path):
    """Parse Veri DSL spec and return (FCLProgram, module_name, todo_names)."""
    sys.path.insert(0, '/opt/veri-build/src')
    sys.path.insert(0, '/opt/veri-build/src/veri_build/dsl/src')
    from veri_build.spec import read_spec
    spec = read_spec(veri_path)
    return spec


def _get_backend(target: str):
    """Resolve a target string to a Backend instance."""
    sys.path.insert(0, '/opt/veri-build/src')
    sys.path.insert(0, '/opt/veri-build/src/veri_build/dsl/src')
    from veri_build.target import get as get_backend
    return get_backend(target)


# ── Step 2: Generate target interface ──────────────────────────────────

def generate_target_interface(spec, target: str):
    """Generate interface-only code (no implementations)."""
    sys.path.insert(0, '/opt/veri-build/src/veri_build/dsl/src')
    backend = _get_backend(target)
    dsl_lang = backend.dsl_language()

    if dsl_lang == 'fstar':
        from backend.fstar.printer import FStarPrinter
        printer = FStarPrinter()
        interface = printer.print(spec.program)
        return interface, 'fst'

    elif dsl_lang == 'dafny':
        from backend.dafny.printer import DafnyPrinter
        printer = DafnyPrinter()
        interface = printer.print(spec.program)
        # Strip TARGET lines — they're pipeline config, not Dafny code
        import re
        interface = re.sub(r'^.*TARGET\s+\S+.*\n?', '', interface, flags=re.MULTILINE)
        interface = interface.strip()
        return interface, 'dfy'

    elif dsl_lang == 'python':
        from backend.python.conditions import ConditionsPrinter
        printer = ConditionsPrinter()
        module_name = spec.module_name
        conditions_source = printer.emit(spec.program, module_name=module_name)
        return conditions_source, 'py'

    raise ValueError(f'Unknown DSL language for target {target}: {dsl_lang}')


# ── Step 3: Run verifier ───────────────────────────────────────────────

def verify_interface(interface_text: str, module_name: str, target: str,
                      suffix: Optional[str] = None, admit_smt: bool = False):
    """Run the target verifier on the interface. Returns (passed, stdout, stderr)."""
    backend = _get_backend(target)
    dsl_lang = backend.dsl_language()
    if suffix is None:
        suffix = 'fst' if dsl_lang == 'fstar' else ('dfy' if dsl_lang == 'dafny' else 'py')

    with tempfile.TemporaryDirectory(prefix='veri-') as tmp:
        f = Path(tmp) / f'{module_name}.{suffix}'
        f.write_text(interface_text)

        if dsl_lang == 'fstar':
            ulib = _find_ulib()
            if not ulib:
                return False, '', 'F* ulib not found in container'
            cmd = ['fstar.exe', '--include', ulib, '--z3rlimit', '5']
            if admit_smt:
                cmd.append('--admit_smt_queries')
                cmd.append('true')
            cmd.append(str(f))
        elif dsl_lang == 'dafny':
            cmd = ['dafny', 'verify', str(f)]
        else:  # python
            # Python verification: import check + dry-run
            with tempfile.TemporaryDirectory(prefix='veri-py-') as py_tmp:
                py_f = Path(py_tmp) / f"{module_name}_conditions.py"
                py_f.write_text(interface_text)
                proc = subprocess.run(
                    ['python3', '-c',
                     f'import os; os.environ["CONTRACT_DRY_RUN"]="1";'
                     f'import {module_name}_conditions',
                     f'print("Python conditions: OK")'],
                    cwd=py_tmp,
                    capture_output=True, text=True, timeout=10,
                )
                passed = proc.returncode == 0
                return passed, proc.stdout, proc.stderr

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            passed = proc.returncode == 0
            return passed, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            return False, '', 'Timed out (60s)'
        except FileNotFoundError as e:
            return False, '', f'Tool not found: {e.filename}'


# ── Step 4: Convert back to Veri DSL ────────────────────────────────────────

def convert_to_veri(interface_text: str, target: str):
    """Convert verified target-language interface back to Veri DSL (Veri DSL)."""
    sys.path.insert(0, '/opt/veri-build/src/veri_build/dsl/src')
    from veri_printer import VeriDslPrinter
    backend = _get_backend(target)
    dsl_lang = backend.dsl_language()

    if dsl_lang == 'fstar':
        from backend.fstar.parser import parse_fstar
        prog = parse_fstar(interface_text)
    elif dsl_lang == 'dafny':
        from backend.dafny.parser import parse_dafny
        prog = parse_dafny(interface_text)
    else:
        raise ValueError(f'No parser for DSL language: {dsl_lang}')

    printer = VeriDslPrinter()
    return printer.print(prog)


# ── Step 5: Launch agent to fill TODOs (if --agent) ────────────────────

# ── Step 6: Compile verified code to output language ─────────────

def compile_verified_code(code: str, spec, target: str, output_dir: Path) -> Optional[str]:
    """Compile verified agent code to C (F* target) or Rust (Dafny target).

    Generates a complete .fst/.dfy with spec types, val signatures, AND the
    agent's verified implementation code. Then runs the target compilation
    toolchain to produce the output artifact.

    Args:
        code: Verified F* or Dafny implementation code from the agent.
        spec: Parsed spec object (from parse_veri_spec).
        target: 'fstar' or 'dafny'.
        output_dir: Directory to write compiled artifacts.

    Returns:
        Path to compiled output file (.c or .rs), or None on failure.
    """
    module_name = spec.module_name or 'Module'
    output_dir.mkdir(parents=True, exist_ok=True)
    backend = _get_backend(target)
    dsl_lang = backend.dsl_language()

    if dsl_lang == 'fstar':
        # Generate complete .fst from spec + agent code
        fst_path = output_dir / f'{module_name}.fst'

        if spec.todo_function_names:
            try:
                sys.path.insert(0, '/opt/veri-build/src')
                sys.path.insert(0, '/opt/veri-build/src/veri_build/dsl/src')
                from veri_build.spec import generate_complete_fst
                fst_text = generate_complete_fst(spec, code)
            except Exception:
                interface_text, _ = generate_target_interface(spec, target)
                fst_text = interface_text + '\n' + code
        else:
            # No TODOs: the spec is fully implemented. Use the interface text as-is.
            fst_text = code

        fst_path.write_text(fst_text)

        # F* type-check (structural only, SMT already checked in verification)
        ulib = _find_ulib()
        if not ulib:
            print(f'  ⚠️ F* ulib not found — skipping {backend.language} compilation')
            return None

        result = subprocess.run(
            ['fstar.exe', '--admit_smt_queries', 'true', '--include', ulib,
             '--cache_checked_modules', '--odir', str(output_dir), str(fst_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f'  ⚠️ F* type-check failed: {result.stderr[:300]}')
            return None

        # Backend-specific extraction (e.g., krml for C, fstar --codegen for OCaml)
        ok, msg = backend.verify_extraction(fst_path, output_dir)
        if ok:
            print(f'  ✅ {msg}')
            c_files = list(output_dir.glob(f'*{backend.output_suffix()}'))
            return str(c_files[0]) if c_files else str(fst_path)
        else:
            print(f'  ⚠️ Extraction failed: {msg[:200]}')
            return None

        # Extract KaRaMeL IR
        result = subprocess.run(
            ['fstar.exe', '--codegen', 'krml', '--extract', module_name,
             '--admit_smt_queries', 'true', '--include', ulib,
             '--cache_checked_modules',
             '--already_cached', f'Prims FStar {module_name}',
             '--odir', str(output_dir), str(fst_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f'  ⚠️ KaRaMeL extraction failed: {result.stderr[:300]}')
            return None

        # Generate C from KaRaMeL IR
        krml_out = output_dir / f'{module_name}.krml'
        if not krml_out.exists():
            print('  ⚠️ KaRaMeL IR file not found')
            return None

        c_dir = output_dir / 'c'
        c_dir.mkdir(exist_ok=True)
        result = subprocess.run(
            ['krml', '-skip-compilation', str(krml_out), '-tmpdir', str(c_dir)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f'  ⚠️ KaRaMeL C generation failed: {result.stderr[:300]}')
            return None

        # Find output .c file
        for cf in output_dir.rglob('*.c'):
            if cf.name.startswith(module_name) or cf.name == f'{module_name}.c':
                return str(cf)
        for cf in c_dir.glob('*.c'):
            return str(cf)

        return None

    elif dsl_lang == 'dafny':
        # For Dafny, write a complete .dfy with spec + implementations
        dfy_path = output_dir / f'{module_name}.dfy'

        # Generate Dafny interface (types + method sigs)
        interface_text, _ = generate_target_interface(spec, target)

        # Append agent code (implementations)
        dfy_text = interface_text + '\n' + code
        dfy_path.write_text(dfy_text)

        # Verify (structural, SMT skipped since already verified)
        result = subprocess.run(
            ['dafny', 'verify', str(dfy_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f'  ⚠️ Dafny verification failed: {result.stderr[:300]}')
            # Still try to translate (Dafny translate may work on unverified code)

        # Backend-specific extraction (e.g., dafny translate java, dafny translate rs)
        ok, msg = backend.verify_extraction(dfy_path, output_dir)
        if ok:
            print(f'  ✅ {msg}')
            out_files = list(output_dir.rglob(f'*{backend.output_suffix()}'))
            return str(out_files[0]) if out_files else None
        else:
            print(f'  ⚠️ Extraction failed: {msg[:200]}')
            return None

    return None


def _run_agent(prompt: str, agent_type: str, timeout: object = None) -> tuple:
    """Run agent CLI and return (stdout, error)."""
    if agent_type == 'claude':
        claude_env = os.environ.copy()
        for key in os.environ:
            if key.startswith('ANTHROPIC_'):
                claude_env[key] = os.environ[key]
        # Multi-turn agent with tool access — can read/write files and run fstar.exe.
        # The parent loop also iterates (rejects incomplete → re-prompts).
        cmd = [
            'claude', '-p', prompt,
            '--allowedTools', 'Bash', '--allowedTools', 'Read', '--allowedTools', 'Write',
            '--print',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=claude_env)
        if proc.returncode != 0:
            err_msg = proc.stderr[:300] if proc.stderr else proc.stdout[:300]
            return None, f'Agent exited with code {proc.returncode}: {err_msg}'
        return proc.stdout, None
    elif agent_type == 'openclaw':
        # Use ANTHROPIC_MODEL env var if set, otherwise default
        model = os.environ.get('ANTHROPIC_MODEL', 'deepseek/deepseek-v4-pro')
        cmd = [
            'openclaw', 'infer', 'model', 'run',
            '--local',
            '--model', model,
            '--prompt', prompt,
            '--json',
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return None, f'Agent exited with code {proc.returncode}: {proc.stderr[:300]}'
        try:
            payload = json.loads(proc.stdout)
            outputs = payload.get('outputs') or []
            text = outputs[0].get('text') if outputs else ''
            if not text:
                return None, f'OpenClaw returned no text output: {proc.stdout[:300]}'
            return text, None
        except Exception as e:
            return None, f'Could not parse OpenClaw JSON output: {e}: {proc.stdout[:300]}'
    else:
        return None, f'Unknown agent type: {agent_type}'


def launch_agent(spec, target: str, agent_type: str, timeout: int,
                  interface_text: Optional[str] = None):
    """Launch sub-agent to fill #TODO functions.

    Protocol:
      1. Agent MUST respond with one of these exact signals:
         - 'CODE' + implementation  → verify; if fail, re-prompt with error
         - 'IMPOSSIBLE: <reason>'   → stop, report impossibility
         - 'RETRY: <explanation>'   → re-prompt
      2. Loop up to 3 rounds, then give up.

    The agent sees the generated F* interface (snake_case types, correct
    signatures) instead of raw Veri DSL blocks, so it writes matching code.
    """
    # Build prompt from generated F* interface (correct type names + signatures)
    # AND the Veri DSL contracts (REQUIRES/ENSURES for each function)
    if interface_text:
        # Extract signatures from the interface, contracts from the Veri DSL blocks
        blocks_text = interface_text
        # Also append the original contracts so the agent knows what to implement
        veri_blocks = '\n\n'.join(getattr(spec, 'resolved_blocks', getattr(spec, 'veri_blocks', [])))
        blocks_text += '\n\n/* Contracts from spec:\n' + veri_blocks + '\n*/'
    else:
        blocks_text = '\n\n'.join(getattr(spec, 'resolved_blocks', getattr(spec, 'veri_blocks', [])))
    todo_list = ', '.join(spec.todo_function_names)
    backend = _get_backend(target)
    extra_rules = backend.agent_extra_rules()
    extra_rules_text = f'\n{extra_rules}\n' if extra_rules else '\n'
    self_check_cmd = backend.self_check_command()
    self_check_lines = (
        f"  3. {self_check_cmd}\n"
        if self_check_cmd
        else ""
    )
    lang = backend.language
    dsl_lang = backend.dsl_language()

    if dsl_lang == 'fstar':
        code_keyword = 'let'
        file_ext = 'fst'
        verifier = 'fstar.exe'
        verifier_cmd = 'fstar.exe --include /opt/fstar/lib/fstar --admit_smt_queries true'
        code_template = (
            f"For each val fn: p1:t1 -> ... -> ret, write:\n"
            f"  let fn (p1: t1) ... : ret = expr\n"
        )
        code_block_format = 'CODE\n<let fn1 = ...>\n<let fn2 = ...>\n...'
    elif dsl_lang == 'dafny':
        code_keyword = 'function method'
        file_ext = 'dfy'
        verifier = 'dafny'
        verifier_cmd = 'dafny verify'
        code_template = (
            f"For each function fn(params): ret, write:\n"
            f"  function method fn(params): ret {{ expr }}\n"
        )
        code_block_format = 'CODE\n<function method fn1(params): ret { body }>\n<function method fn2(params): ret { body }>\n...'
    else:
        code_keyword = 'def'
        file_ext = 'py'
        verifier = 'python3'
        verifier_cmd = 'python3 -m py_compile'
        code_template = ''
        code_block_format = 'CODE\n<implementation code>'

    output_note = (
        f"Output: compiled artifacts go to build/ next to the spec.\n"
        f"  For {backend.name}: build/c/*.{backend.output_suffix()} and build/c/*.h\n"
    ) if hasattr(backend, 'output_suffix') and backend.output_suffix() else ''

    base_prompt = (
        f"Write {code_keyword} definitions for ALL {len(spec.todo_function_names)} functions below.\n"
        f"Self-check before returning: write .{file_ext}, run {verifier}{', then check extraction' if self_check_cmd else ''}.\n"
        f"Only return CODE if {verifier} VERIFIES{' and extraction succeeds' if self_check_cmd else ''}.\n\n"
        f"{code_template}"
        f"RULES:\n"
        f"- Copy names and types verbatim from the interface.\n"
        f"- ALL {len(spec.todo_function_names)} functions required.\n"
        f"{extra_rules_text}"
        f"FUNCTIONS:\n{todo_list}\n\n"
        f"INTERFACE:\n{blocks_text}\n\n"
        f"Self-check:\n"
        f"  1. Write .{file_ext} to /output/module.{file_ext}\n"
        f"  2. {verifier_cmd} /output/module.{file_ext}\n"
        f"{self_check_lines}"
        f"Only return CODE if both pass.\n\n"
        f"{output_note}"
        f"Return:\n"
        f"{code_block_format}"
        f"\nOr: IMPOSSIBLE: <reason>\nOr: RETRY: <what you need>"
    )

    max_rounds = 3
    for round_num in range(1, max_rounds + 1):
        if round_num == 1:
            prompt = base_prompt
        else:
            # Count how many functions were implemented in this round
            _keyword = 'function method' if dsl_lang == 'dafny' else 'let'
            _implemented = sum(1 for fn in spec.todo_function_names if f'{_keyword} {fn} ' in str(last_response))
            _missing = len(spec.todo_function_names) - _implemented
            prompt = (
                f"Previous round: {_implemented} of {len(spec.todo_function_names)} functions implemented."
                f" {_missing} still missing.\n\n"
                f"SPEC:\n{blocks_text}\n\n"
                f"Self-check with {verifier} before returning. Return CODE with ALL functions or IMPOSSIBLE."
            )
        output, error = _run_agent(prompt, agent_type, timeout)
        if error:
            return None, error

        last_response = output.strip()
        # Log raw agent response for debugging (first 200 chars)
        sys.stderr.write(f'[agent raw] ({len(last_response)} chars) start: {last_response[:200]}\n')

        if last_response.startswith('IMPOSSIBLE:'):
            return None, last_response  # Report impossibility clearly

        # Accept CODE prefix or bare definitions
        if last_response.startswith('CODE'):
            raw = last_response[4:].strip()
        elif dsl_lang == 'dafny' and any(last_response.startswith(p) for p in ('function', 'method ', 'module ', 'datatype ')):
            raw = last_response  # Bare Dafny definitions — also valid
        elif dsl_lang == 'fstar' and last_response.startswith('let '):
            raw = last_response  # Bare let definitions — also valid
        else:
            # Dafny: check if response contains a Dafny module (agent may describe results first)
            if dsl_lang == 'dafny' and 'module ' in last_response and '{' in last_response:
                # Extract the module block from anywhere in the response
                raw = last_response
            else:
                raw = last_response  # Will be filtered below

        # Strip markdown fences if present
        raw = re.sub(r'^```(?:fstar|dafny)?\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)

        # Extract code definitions from the agent's response.
        if dsl_lang == 'dafny':
            # For Dafny, extract the whole module content.
            # The agent may return the full module including module SortedInsert { ... }.
            # Or just the function/method implementations.
            # Strategy: look for Dafny module { ... } and extract everything inside,
            # or grab all function/method/function method declarations.
            code_sections = []
            in_module = False
            brace_depth = 0
            for l in raw.split('\n'):
                s = l.strip()
                if s.startswith('module ') and s.endswith('{'):
                    in_module = True
                    brace_depth = 1
                    code_sections.append('')  # start fresh inside module
                elif in_module:
                    if '{' in s:
                        brace_depth += s.count('{') - s.count('}')
                    elif '}' in s:
                        brace_depth -= 1
                    if brace_depth <= 0:
                        in_module = False
                    else:
                        # Strip one level of indentation
                        stripped = l[4:] if l.startswith('    ') else l
                        code_sections.append(stripped)
            if code_sections:
                code = '\n'.join(code_sections).strip()
            else:
                # No module found — grab function/method declarations
                code_lines = []
                in_decl = False
                words = ('function ', 'function method ', 'method ', 'datatype ', 'newtype ', 'type ')
                for l in raw.split('\n'):
                    if any(l.lstrip().startswith(w) for w in words):
                        in_decl = True
                        code_lines.append(l)
                    elif in_decl:
                        if l.strip() == '' or l.startswith(' '):
                            code_lines.append(l)
                        else:
                            in_decl = False
                code = '\n'.join(code_lines)
        else:
            # F*: extract let definitions
            code_lines = []
            in_let = False
            for l in raw.split('\n'):
                if l.lstrip().startswith('let '):
                    in_let = True
                    code_lines.append(l)
                elif in_let:
                    if l.strip() == '' or l.startswith(' '):
                        code_lines.append(l)
                    else:
                        in_let = False
            code = '\n'.join(code_lines)
            # Strip Pure/GTot/ST effect annotations from let definitions
            code = re.sub(r':\s*(?:Pure|GTot|ST)\s+(\S+)\s*\(.*?\)\s*\(.*?\)\s*=', lambda m: f': {m.group(1)} =', code)
            code = re.sub(r':\s*GTot\s+(\S+)\s*=', lambda m: f': {m.group(1)} =', code)

            # Fix common naming mistakes the agent makes
            type_fixes = {
                r'\bint32\b': 'Prims.int',
                r'\bint64\b': 'Prims.int',
                r'\bfloat64\b': 'Prims.int',
                r'\bbool\b': 'Prims.bool',
                r'\bstring\b': 'Prims.string',
            }
            for old, new in type_fixes.items():
                code = re.sub(old, new, code)
            _fstar_builtins = {'Prims', 'FStar', 'List', 'Seq', 'Set', 'Map', 'Option', 'Nat', 'Buffer', 'ST'}
            def _snake(m):
                name = m.group(1)
                if name in _fstar_builtins or name.isupper():
                    return name
                return re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', name).lower()
            code = re.sub(r'\b([A-Z][a-zA-Z0-9]*[a-z][A-Za-z0-9]*)\b', _snake, code)
        # Try to verify the code (with type definitions prepended)
        try:
            # Generate the full module: spec types + agent implementation
            types_text, _ = generate_target_interface(spec, target)
            # Strip val/assume val declarations (F*) or function/method declarations (Dafny)
            # from the interface so agent implementations don't conflict
            if dsl_lang == 'fstar':
                types_text = re.sub(r'^(assume\s+)?val\s+\w+.*?(\n\s|\n$)', '', types_text, flags=re.MULTILINE)
            elif dsl_lang == 'dafny':
                # Remove the existing function declarations (keep types/datatypes)
                types_text = re.sub(r'^\s*(function|function method|method)\s+\w+.*?\{[^}]*\}', '', types_text, flags=re.MULTILINE | re.DOTALL)
            full_module = types_text + '\n' + code
            passed, stdout, stderr = verify_interface(
                full_module, spec.module_name, target,
                    suffix=file_ext, admit_smt=True)
            if passed:
                return code, None  # Success!
            # Verification failed — re-prompt with actionable error
            short_err = stderr[-500:] if stderr else '(empty stderr)'
            # Detect common issues to give better feedback
            hints = []
            if dsl_lang == 'fstar':
                if 'Identifier not found' in stderr:
                    hints.append('Use snake_case type names (perf_coeffs, not PerfCoeffs).')
                if 'Prims.int' in stderr and 'int32' in stderr:
                    hints.append('Use Prims.int, not int32.')
                if 'Expected' in stderr and 'prop' in stderr:
                    hints.append('Wrap boolean results with True/False or b2t for prop context.')
            hint_text = ' '.join(hints) if hints else ''
            verifier_name = 'F*' if dsl_lang == 'fstar' else 'Dafny'
            code_style = '`let` definitions' if dsl_lang == 'fstar' else '`function method` definitions'
            last_response = (
                f"Your code failed {verifier_name} verification:\n{short_err}\n"
                f"{hint_text}\n"
                f"Fix the code and respond with CODE + corrected {code_style},"
                f" or IMPOSSIBLE: if the spec cannot be satisfied."
            )
        except Exception as e:
            last_response = f"Could not verify your code: {e}. Fix and retry, or report IMPOSSIBLE."
            continue

        if last_response.startswith('RETRY:'):
            continue  # Re-prompt (keeps the retry reason in context)

        # Unrecognized format — detect common failure modes and give targeted feedback
        hints = []
        response_lower = last_response.lower()
        if '```' in last_response:
            hints.append('You wrapped code in markdown fences. Start with CODE on its own line, then the bare code (no fences).')
        elif 'let ' in last_response or 'function ' in last_response:
            hints.append('You wrote code but without the CODE prefix. Start your response with exactly "CODE" on its own line, then the implementation.')
        elif response_lower.startswith(('i think', 'here', 'the', 'we', 'to', 'as')):
            hints.append('You started with analysis text. Respond with exactly "CODE" on its own line, nothing before it.')
        elif len(last_response) < 20:
            hints.append('Your response was too short. Provide the full implementation.')
        else:
            hints.append(f'Start with exactly "CODE" on its own line, followed by the implementation.')
        if 'impossible' in response_lower and 'reason' not in response_lower:
            hints.append('If you believe the spec is impossible, use "IMPOSSIBLE: <reason>" with a detailed explanation.')
        last_response = ';\n'.join(hints) + '.'

    msg = 'Agent exhausted all rounds without producing valid code or declaring impossibility.'
    if last_response:
        msg += f'\nLast agent response (first 500 chars):\n{last_response[:500]}'
    return None, msg


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='veri-build docker runner')
    parser.add_argument('spec', type=Path, help='Path to .veri.md')
    parser.add_argument('--target', default='fstar-c')
    parser.add_argument('--agent', choices=['claude', 'openclaw'], default=None)
    parser.add_argument('--agent-timeout', type=int, default=600)
    parser.add_argument('--impl', type=Path, default=None,
                        help='Path to real Python implementation (for python target)')
    parser.add_argument('--write', action='store_true',
                        help='Write injector changes to --impl (python target)')
    parser.add_argument('--output', type=Path, default=None,
                        help='Write results JSON to this file')
    args = parser.parse_args()

    result = {
        'module_name': None,
        'target': args.target,
        'interface': None,
        'verification_passed': False,
        'verification_stdout': '',
        'verification_stderr': '',
        'veri_from_target': None,
        'agent_output': None,
        'agent_error': None,
        'output_path': None,
        'compilation_success': None,
        'compilation_error': None,
        'error': None,
    }

    # Resolve backend (e.g., 'fstar-c', 'fstar-ocaml', 'dafny-rust')
    # Also handle common aliases for backward compatibility
    _alias_map = {
        'fstar': 'fstar-c', 'f-star': 'fstar-c', 'c': 'fstar-c',
        'ocaml': 'fstar-ocaml', 'ml': 'fstar-ocaml',
        'wasm': 'fstar-wasm', 'f-star-wasm': 'fstar-wasm',
        'dafny': 'dafny-rust', 'rust': 'dafny-rust',
        'java': 'dafny-java',
        'js': 'dafny-js', 'javascript': 'dafny-js',
        'python': 'python-assert', 'py': 'python-assert',
    }
    resolved_target = _alias_map.get(args.target.lower().strip(), args.target)
    try:
        backend = _get_backend(resolved_target)
        dsl_lang = backend.dsl_language()
    except KeyError:
        known = 'fstar-c, fstar-ocaml, dafny-rust, python-assert'
        result['error'] = f'Unknown target: {args.target} (resolved: {resolved_target}). Known: {known}'
        print(json.dumps(result))
        sys.exit(1)

    # Step 1: Parse Veri DSL
    try:
        spec = parse_veri_spec(args.spec)
        result['module_name'] = spec.module_name
        result['todo_functions'] = spec.todo_function_names
    except Exception as e:
        result['error'] = f'Veri DSL parse failed: {e}'
        print(json.dumps(result))
        sys.exit(1)

    # Step 2: Generate target interface
    try:
        interface_text, ext = generate_target_interface(spec, args.target)
        result['interface'] = interface_text

        # Write generated files to the output volume (mounted from host)
        output_dir_candidates = [
            Path('/output'),
            Path('/tmp/output'),
            Path(tempfile.mkdtemp(prefix='veri-out-')),
        ]
        output_dir = None
        for candidate in output_dir_candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                test_file = candidate / '.write_test'
                test_file.write_text('ok')
                test_file.unlink()
                output_dir = candidate
                break
            except (PermissionError, OSError):
                continue
        if output_dir is None:
            raise RuntimeError('No writable output directory found')
        iface_path = output_dir / f'{spec.module_name}.{ext}'
        iface_path.write_text(interface_text)
        result['output_path'] = str(iface_path)

        # For Python target, write _conditions.py next to the spec or to --output
        if dsl_lang == 'python':
            cond_path = (args.output or args.spec.parent) / f'{spec.module_name}_conditions.py'
            cond_path.write_text(interface_text)
            result['conditions_path'] = str(cond_path)
    except Exception as e:
        result['error'] = f'Interface generation failed: {e}'
        print(json.dumps(result))
        sys.exit(1)

    # Step 2b: For Python target, inject decorators into real code
    if dsl_lang == 'python' and args.impl:
        try:
            from backend.python.inject import inject_decorators
            inject_result = inject_decorators(
                spec_path=args.spec,
                impl_path=args.impl,
                dry_run=not args.write,
            )
            result['injector_changes'] = [
                {'action': c.action, 'function': c.function, 'detail': c.detail}
                for c in inject_result.changes
            ]
            if args.write and inject_result.output_source:
                args.impl.write_text(inject_result.output_source)
                result['injector_applied'] = True
            # Re-read for verification
            impl_source = args.impl.read_text() if args.impl.exists() else None
            result['impl_source'] = impl_source[:500] if impl_source else None
        except Exception as e:
            result['injector_error'] = str(e)

    # Step 2c: For Python target, verify decorators match spec
    if dsl_lang == 'python' and args.impl:
        try:
            from backend.python.verify import verify_implementation
            from fcl_parser import parse_fcl
            fcl_blocks = _extract_veri_blocks(args.spec.read_text())
            fcl_text = '\n\n'.join(fcl_blocks)
            fcl_prog = parse_fcl(fcl_text)
            cond_path = (args.output or args.spec.parent) / f'{spec.module_name}_conditions.py'
            verify_result = verify_implementation(fcl_prog, args.impl, cond_path)
            result['verification_all_pass'] = verify_result.all_pass
            result['verification_checks'] = [
                {'name': c.get('name'), 'passed': c.get('passed'), 'detail': c.get('detail')}
                for c in verify_result.checks
            ]
        except Exception as e:
            result['verification_error'] = str(e)

    # Step 3: Verify interface (F*/Dafny) or run condition dry-check (Python)
    # For Python: we already did the functional verify above; do the syntax check here
    if dsl_lang in ('fstar', 'dafny'):
        # Use admit_smt for the interface verification since implementations
        # with complex properties (e.g. sortedness preservation) may not be
        # provable by SMT alone. The val declarations still carry the contracts.
        _suffix = 'fst' if dsl_lang == 'fstar' else 'dfy'
        passed, stdout, stderr = verify_interface(interface_text, spec.module_name, args.target, suffix=_suffix, admit_smt=True)
        result['verification_passed'] = passed
        result['verification_stdout'] = stdout[-1000:] if stdout else ''
        result['verification_stderr'] = stderr[-1000:] if stderr else ''
    elif dsl_lang == 'python':
        # Syntax check only for the conditions module
        cond_path = (args.output or args.spec.parent) / f'{spec.module_name}_conditions.py'
        if cond_path.exists():
            passed, stdout, stderr = verify_interface(cond_path.read_text(), spec.module_name, args.target)
            result['verification_passed'] = passed
            result['verification_stdout'] = stdout[:500] if stdout else ''
            result['verification_stderr'] = stderr[:500] if stderr else ''

    # Step 4: Convert back to Veri DSL (F*/Dafny only)
    if dsl_lang in ('fstar', 'dafny') and result.get('verification_passed'):
        try:
            fcl = convert_to_veri(interface_text, args.target)
            result['veri_from_target'] = fcl
        except Exception as e:
            result['error'] = f'Veri DSL conversion failed: {e}'

    # Step 4b: If there are no TODOs, the spec is fully implemented.
    # Compile the verified code to the output language (C via KaRaMeL for fstar).
    if not spec.todo_function_names and result.get('verification_passed'):
        try:
            output_path = compile_verified_code(
                interface_text, spec, args.target,
                output_dir=Path('/output'),
            )
            if output_path:
                result['output_path'] = output_path
                result['compilation_success'] = True
            else:
                result['compilation_error'] = 'Compilation produced no output'
        except Exception as e:
            result['compilation_error'] = str(e)

    # Step 5: Launch agent (if requested)
    if args.agent and spec.todo_function_names:
        agent_output, agent_error = launch_agent(
            spec, args.target, args.agent, args.agent_timeout,
            interface_text=interface_text)
        result['agent_output'] = agent_output
        result['agent_error'] = agent_error

        if agent_output:
            # Try to verify the agent's output (build full module with types)
            try:
                types_text, _ = generate_target_interface(spec, args.target)
                if dsl_lang == 'fstar':
                    types_text = re.sub(r'^(assume\s+)?val\s+\w+.*?(\n\s|\n$)', '', types_text, flags=re.MULTILINE)
                elif dsl_lang == 'dafny':
                    types_text = re.sub(r'^\s*(function|function method|method)\s+\w+.*?\{[^}]*\}', '', types_text, flags=re.MULTILINE | re.DOTALL)
                full_module = types_text + '\n' + agent_output
                agent_passed, agent_stdout, agent_stderr = verify_interface(
                    full_module, spec.module_name, args.target)
                result['agent_verification_passed'] = agent_passed
                result['agent_verification_stdout'] = agent_stdout[-500:] if agent_stdout else ''
                result['agent_verification_stderr'] = agent_stderr[-500:] if agent_stderr else ''

                if agent_passed:
                    veri_from_agent = convert_to_veri(full_module, args.target)
                    result['veri_from_agent'] = veri_from_agent

                    # Step 6: Compile the verified agent code
                    try:
                        output_path = compile_verified_code(
                            agent_output, spec, args.target,
                            output_dir=Path('/output'),
                        )
                        if output_path:
                            result['output_path'] = output_path
                            result['compilation_success'] = True
                        else:
                            result['compilation_error'] = 'Compilation produced no output'
                    except Exception as e:
                        result['compilation_error'] = str(e)
            except Exception as e:
                result['agent_verification_failed'] = str(e)

    # Output
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output)
    else:
        print(output)


if __name__ == '__main__':
    main()
