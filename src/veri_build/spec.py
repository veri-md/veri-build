"""Spec — Read .veri.md files, extract Veri DSL blocks, merge into VeriDslProgram AST,
convert to F* .fst for interface checking, and .fst with stubs for TODO filling."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from veri_ast import VeriDslProgram, ModuleDecl, QualifiedIdent, Ident, TypeVar, Binder
from veri_parser import parse_veri as parse_veri_text
from backend.fstar.printer import FStarPrinter


VERI_BLOCK_RE = re.compile(r'```veri\n(.*?)```', re.DOTALL)


@dataclass
class ExtractedSpec:
    """Parsed .veri.md — the full content needed for the pipeline."""
    path: Path
    raw_text: str
    veri_blocks: List[str]              # Raw Veri DSL text from each ```veri block (before import resolution)
    resolved_blocks: List[str]           # Blocks after import resolution (inlined type defs)
    program: VeriDslProgram                # Merged AST from all blocks
    module_name: str
    todo_function_names: List[str]     # Functions marked #TODO
    todo_indices: List[int]            # Which blocks contain TODOs
    veri_version: Optional[str] = None  # VERI_VERSION from spec (e.g., '0.3.0')


def extract_blocks(md_text: str) -> List[str]:
    """Extract all ```veri fenced blocks from markdown text."""
    return [b.strip() for b in VERI_BLOCK_RE.findall(md_text) if b.strip()]


def detect_todo_blocks(blocks: List[str]) -> List[int]:
    """Return indices of blocks containing #TODO (with or without space after #)."""
    return [i for i, b in enumerate(blocks) if '#TODO' in b or '# TODO' in b]


def extract_todo_names(block: str) -> List[str]:
    """Extract the names of the *unimplemented* functions in a #TODO block.

    A block is marked for filling with a trailing ``#TODO``, but it may also
    hold fully-implemented helpers (a ``def`` with a ``return``). Only the
    body-less defs — a contract but no body — are holes the agent must fill; a
    bodied def sharing the block is already implemented and must not be listed
    (it would be needlessly re-implemented and, when emitted bodied in the
    interface, mis-stitched).
    """
    lines = block.split('\n')
    starts = []
    for i, line in enumerate(lines):
        m = re.match(r'^\s*def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', line)
        if m:
            starts.append((i, m.group(1)))

    names = []
    for k, (i, name) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        has_body = any(re.match(r'^\s*return\b', s) for s in lines[i:end])
        if not has_body:
            names.append(name)
    return names


def merge_programs(blocks: List[str], module_name: str) -> VeriDslProgram:
    """Parse each Veri DSL block and merge into one program.

    Each block is parsed independently with veri_parser.
    Results are merged: module name set to the canonical name,
    all declarations collected.

    Supports graceful fallback for F*-only blocks (records, etc.):
    blocks that fail Veri DSL parsing are kept as opaque F* declarations.
    """
    from veri_parser import parse_veri as _parse_veri
    
    merged = VeriDslProgram()
    merged.module = ModuleDecl(name=QualifiedIdent([module_name]))

    for i, block_text in enumerate(blocks):
        import re as _re
        cleaned = _re.sub(r'^\s*#TODO.*$', '', block_text, flags=_re.MULTILINE)
        cleaned = _re.sub(r'^\s*# TODO.*$', '', cleaned, flags=_re.MULTILINE)
        cleaned = cleaned.strip()
        
        # Try Veri DSL parser first
        try:
            program = _parse_veri(cleaned)
            for decl in program.decls:
                merged.add(decl)
            continue
        except (SyntaxError, Exception):
            pass
        
        # Fallback: emit opaque text block for non-Veri DSL content
        # (kept as-is for verifiability, skipped during target codegen)
        from veri_ast import PragmaDecl
        merged.add(PragmaDecl(text=cleaned))

    return merged


def _extract_module_name_from_blocks(blocks: List[str]) -> Optional[str]:
    """Look for an explicit `module <Name>` declaration in Veri DSL blocks.

    Scans each block for a line matching `module <Identifier>` (skipping
    comments and blank lines). Returns the name if found, None otherwise.
    """
    module_re = re.compile(r'^\s*module\s+([a-zA-Z_][a-zA-Z0-9_]*)')
    for block in blocks:
        for line in block.split('\n'):
            stripped = line.strip()
            # Skip comments, blank lines, and non-module lines
            if not stripped or stripped.startswith('#'):
                continue
            m = module_re.match(stripped)
            if m:
                return m.group(1)
    return None


def _resolve_imports(blocks: List[str], md_path: Path) -> List[str]:
    """Resolve `import X` statements by inlining type definitions from X.veri.md.

    For each `import X` line in a Veri DSL block, find the corresponding
    X.veri.md file, extract its type declarations (class, type alias,
    CONSTRAINT, and the FStar.Seq import), and insert them into the block.
    This avoids F* cross-module dependencies in the lint step.
    """
    result = []
    for block in blocks:
        lines = block.split('\n')
        new_lines = []
        for line in lines:
            stripped = line.strip()
            m = re.match(r'^import\s+(\w+)$', stripped)
            if not m:
                new_lines.append(line)
                continue

            # Resolve X.veri.md relative to the importing spec's directory
            imported_name = m.group(1)
            # Try exact name first
            import_path = md_path.parent / f'{imported_name}.veri.md'
            if not import_path.exists():
                # CamelCase → snake_case: DeadlineModel → deadline_model
                snake = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', imported_name).lower()
                import_path = md_path.parent / f'{snake}.veri.md'
            if not import_path.exists():
                new_lines.append(f'# ERROR: imported spec "{imported_name}" not found at {import_path}')
                continue

            # Read the imported spec and extract only type declarations
            import_text = import_path.read_text()
            import_blocks = extract_blocks(import_text)
            type_decls = []
            for ib in import_blocks:
                ib_lines = ib.split('\n')
                filtered = []
                for il in ib_lines:
                    s = il.strip()
                    # Keep: TARGET, import, class, type, CONSTRAINT
                    if (s.startswith(('TARGET', 'class ', 'type ', 'CONSTRAINT '))
                        or s.startswith('import ')
                        or s.startswith('#')
                        or s == ''):
                        filtered.append(il)
                    # Also keep indented field lines inside classes
                    elif il.startswith((' ', '\t')):
                        filtered.append(il)
                    # Stop at def statements (functions)
                    elif s.startswith('def '):
                        break
                if filtered:
                    type_decls.extend(filtered)

            if type_decls:
                # Don't add a comment about the import — the agent prompt would try
                # to resolve the file path, but the types are already inlined.
                new_lines.extend(type_decls)
            else:
                new_lines.append(f'# {imported_name}.veri.md found but no type declarations extracted')

        result.append('\n'.join(new_lines))
    return result


def read_spec(md_path: Path, module_name: Optional[str] = None) -> ExtractedSpec:
    """Read and parse a .veri.md file.

    Module name resolution (precedence):
      1. Explicit `module_name` argument
      2. Explicit `module <Name>` declaration in a ```veri block
      3. Derived from filename: "html_entity_decoder.veri.md" → "HtmlEntityDecoder"

    Args:
        md_path: Path to .veri.md file
        module_name: Override module name (default: derived from filename)

    Returns:
        ExtractedSpec with parsed Veri DSL program, TODO info, and raw blocks

    Raises:
        SyntaxError: If Veri DSL parsing fails
        FileNotFoundError: If .veri.md doesn't exist
    """
    if not md_path.exists():
        raise FileNotFoundError(f"Spec not found: {md_path}")

    raw = md_path.read_text()
    raw_blocks = extract_blocks(raw)

    # Resolve `import X` by inlining type declarations from X.veri.md
    blocks = _resolve_imports(raw_blocks, md_path)

    if not blocks:
        raise ValueError(
            f"No ```veri blocks found in {md_path}. "
            f"Spec must contain at least one fenced code block."
        )

    if module_name is None:
        # 1. Check for explicit module declaration in Veri DSL blocks
        module_name = _extract_module_name_from_blocks(blocks)

    if module_name is None:
        # 2. Derive from filename: "sorted_list.veri.md" → "SortedList"
        stem = md_path.stem  # e.g. "sorted_list.veri"
        if stem.endswith('.veri'):
            stem = stem[:-5]
        module_name = ''.join(p.capitalize() for p in stem.replace('-', '_').split('_'))

    todo_indices = detect_todo_blocks(blocks)
    todo_names = []
    for idx in todo_indices:
        todo_names.extend(extract_todo_names(blocks[idx]))

    program = merge_programs(blocks, module_name)

    # Extract VERI_VERSION from raw text
    veri_version = None
    version_match = re.search(r'```veri\n.*?VERI_VERSION\s+(\S+)', raw, re.DOTALL)
    if version_match:
        veri_version = version_match.group(1).strip()

    return ExtractedSpec(
        path=md_path,
        raw_text=raw,
        veri_blocks=raw_blocks,
        resolved_blocks=blocks,
        program=program,
        module_name=module_name,
        todo_function_names=todo_names,
        todo_indices=todo_indices,
        veri_version=veri_version,
    )


def generate_interface(spec: ExtractedSpec) -> str:
    """Generate F* .fst (interface) from the spec.

    The .fst contains type declarations and val signatures only —
    no let-bindings, no admit(), no stubs. This is what F* can
    check purely as an interface.
    """
    printer = FStarPrinter()
    return printer.print(spec.program)


def generate_fst_with_stubs(spec: ExtractedSpec) -> str:
    """Generate a .fst with admit() stubs for all TODO functions.

    .fst has: module header + open/type declarations + admit() stubs.
    No val declarations — those are in the .fst pair file.

    Deprecated: use generate_complete_fst() instead, which preserves
    val signatures and accepts real implementation code.
    """
    from veri_ast import (
        ValDecl, OpenDecl, TypeAlias, TypeAbstract, TypeRecord, TypeVariant,
    )
    from backend.fstar.printer import FStarPrinter

    printer = FStarPrinter()
    ast = VeriDslProgram()
    ast.module = spec.program.module

    for decl in spec.program.decls:
        if decl.__class__.__name__ in ('ValDecl',):
            continue
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


def generate_complete_fst(spec: ExtractedSpec, impl_code: str = "") -> str:
    """Generate a complete .fst with spec types, val signatures, and real implementations.

    The generated .fst contains everything needed for KaRaMeL extraction:
      - Module header and open declarations
      - ALL type declarations from the spec (records, variants, aliases, abstracts)
      - ALL val declarations with full REQUIRES/ENSURES specs
      - Provided implementation let-bindings (instead of admit() stubs)

    Unlike generate_fst_with_stubs(), this function:
      1. Includes val declarations so the spec's contracts are preserved
      2. Uses real implementation code instead of admit() stubs
      3. Produces a .fst ready for direct KaRaMeL extraction

    Args:
        spec: Parsed Veri DSL spec
        impl_code: F* let-bindings to use as implementations.
                   Should be valid F* code that satisfies the val signatures.
                   If empty, falls back to admit() stubs.

    Returns:
        Complete .fst source code with: types + val sigs + implementations
    """
    from backend.fstar.printer import FStarPrinter

    printer = FStarPrinter()

    # Generate the full program — types AND val signatures
    fst_text = printer.print(spec.program)
    fst_text += "\n"

    if impl_code and impl_code.strip():
        # Use the provided implementation code directly
        fst_text += impl_code.strip()
    else:
        # Fall back to admit() stubs for backward compatibility
        from veri_ast import ValDecl
        for fn_name in spec.todo_function_names:
            for decl in spec.program.decls:
                if isinstance(decl, ValDecl) and decl.name == fn_name:
                    param_count = len(decl.params)
                    params = ' '.join([f'x{i}' for i in range(param_count)])
                    fst_text += f'let {fn_name} {params} = admit()\n'
                    break

    return fst_text
CHILD_AGENT_SYSTEM_PROMPT = """You are a verification child agent running inside a Docker sandbox.

RULES:
  1. You have READ-ONLY access to /workspace/ (the project files).
     You can read the .veri.md spec but must not modify it.
  2. Write your F*/Dafny code in a temporary .veri.f.md or .veri.dfy.md file.
     These are ephemeral — recreated each run.
  3. Never write Veri DSL directly. Veri DSL is for the user only.
  4. After writing the target-language code, run the verifier:
     - For F*: fstar.exe --include /opt/fstar/lib/fstar/ulib <file>.fst
     - For Dafny: dafny verify <file>.dfy
  5. If verification fails, fix the code and retry (up to 3 times).
  6. Once verified, the pipeline converts your code to Veri DSL for the user.
     You do not need to do the conversion yourself.
  7. The parent can add context below. Pay attention to it.
"""

def spec_to_prompt(spec: ExtractedSpec, target: str = 'fstar',
                   parent_context: str = '') -> str:
    """Build the LLM prompt for filling TODOs.

    The LLM (child agent) works in F*/Dafny — never in Veri DSL.
    The prompt includes the fixed system prompt, the spec, optional
    parent context, and instructions.

    Args:
        spec: Parsed Veri DSL spec
        target: 'fstar' or 'dafny'
        parent_context: Optional extra context from parent orchestrator

    Returns:
        Full prompt string for the child agent
    """
    blocks_text = '\n\n'.join(spec.veri_blocks)
    todo_list = ', '.join(spec.todo_function_names)

    if target == 'fstar':
        lang = 'F*'
        verify_cmd = 'fstar.exe --include /opt/fstar/lib/fstar/ulib'
        ext = 'fsti'
    else:
        lang = 'Dafny'
        verify_cmd = 'dafny verify'
        ext = 'dfy'

    parts = [CHILD_AGENT_SYSTEM_PROMPT]

    if parent_context:
        parts.append(f'[Context from parent]\n{parent_context}\n[End context]')

    parts.append(
        f'The spec ({spec.module_name}):\n\n'
        f'{blocks_text}\n\n'
        f'---\n\n'
        f'Functions marked #TODO: {todo_list}\n'
        f'Write the {lang} code in a temp file (spec.veri.{ext.split(".")[0] if ext != "dfy" else "dfy"}.md).\n'
        f'Run: {verify_cmd} on the generated interface file to verify.\n'
        f'Return ONLY the {lang} code. No markdown formatting, no explanation,'
        f' no code fences.'
    )

    return '\n\n'.join(parts)
