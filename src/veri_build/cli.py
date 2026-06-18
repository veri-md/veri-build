"""CLI — Argument parsing and command dispatch for veri-build."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from . import __version__
from .lint import lint_interface, lint_with_stubs, auto_tune_rlimit, find_fstar, LintError
from .fill import fill_todos, FillError
from .spec import read_spec, ExtractedSpec


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with lint and verify subcommands."""
    parser = argparse.ArgumentParser(
        prog="veri-build",
        description="Formally verify Veri DSL specs, fill implementations, and compile to WASM.",
        epilog="See README.md for examples and full documentation.",
    )
    parser.add_argument(
        "--version", action="version", version=f"veri-build {__version__}"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- lint ----
    lint_p = sub.add_parser("lint", help="Verify interface: Veri DSL → .fst → fstar.exe")
    lint_p.add_argument("spec", type=Path, help="Path to .veri.md file")
    lint_p.add_argument("--module-name", help="Module name (default: derived from filename)")
    lint_p.add_argument("--fstar-bin", help="Path to fstar.exe")
    lint_p.add_argument("--rlimit", type=int, default=5, help="Z3 resource limit (default: 5)")
    lint_p.add_argument("--fuel", type=int, default=2, help="SMT fuel (default: 2)")
    lint_p.add_argument("--auto-rlimit", action="store_true", help="Auto-tune rlimit if interface check fails")

    # ---- compile ----
    compile_p = sub.add_parser("compile", help="Full pipeline: lint → fill → verify → compile")
    compile_p.add_argument("spec", type=Path, help="Path to .veri.md file")
    compile_p.add_argument("--module-name", help="Module name (default: derived from filename)")
    compile_p.add_argument("--fstar-bin", help="Path to fstar.exe")
    compile_p.add_argument("--child", choices=["claude", "pi"], default="claude",
                          help="LLM CLI for TODO filling (default: claude)")
    compile_p.add_argument("--target", choices=["wasm", "so", "none"], default="none",
                          help="Compilation target (default: none)")
    compile_p.add_argument("--out-dir", type=Path, default=Path("build"),
                          help="Output directory for build artifacts (default: build/)")
    compile_p.add_argument("--rlimit", type=int, default=5, help="Initial Z3 rlimit (default: 5)")
    compile_p.add_argument("--fuel", type=int, default=2, help="SMT fuel (default: 2)")
    compile_p.add_argument("--auto-rlimit", action="store_true",
                          help="Auto-tune rlimit if proof fails")
    compile_p.add_argument("--retries", type=int, default=3,
                          help="LLM retry attempts (default: 3)")
    compile_p.add_argument("--timeout", type=int, default=30,
                          help="LLM subprocess timeout in seconds (default: 30)")

    return parser


def cmd_lint(args: argparse.Namespace) -> int:
    """Run interface verification (Hook 1)."""
    try:
        print(f"📖 Parsing {args.spec.name}...")
        spec = lint_interface(
            md_path=args.spec,
            module_name=args.module_name,
            fstar_bin=args.fstar_bin,
            rlimit=args.rlimit,
            fuel=args.fuel,
        )
        print(f"✅ Interface check passed — {spec.module_name}")

        if spec.todo_function_names:
            print(f"   TODO functions ({len(spec.todo_function_names)}): "
                  f"{', '.join(spec.todo_function_names)}")

        return 0

    except LintError as e:
        print(f"❌ {e.message}", file=sys.stderr)

        if args.auto_rlimit:
            print(f"\n🔧 Auto-tuning rlimit...")
            best = auto_tune_rlimit(
                args.spec, args.module_name, args.fstar_bin
            )
            print(f"   Best rlimit found: {best}")
            if best != args.rlimit:
                print(f"   Re-run with: veri-build lint {args.spec} --rlimit {best}")
        return 1

    except (SyntaxError, ValueError, FileNotFoundError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


def cmd_compile(args: argparse.Namespace) -> int:
    """Full pipeline: lint → fill → verify → compile."""
    try:
        # ── Step 1: Lint interface (pure F* check, no admits) ──
        print(f"📖 Step 1: Parsing {args.spec.name}...")
        try:
            spec = lint_interface(
                md_path=args.spec,
                module_name=args.module_name,
                fstar_bin=args.fstar_bin,
                rlimit=args.rlimit,
                fuel=args.fuel,
            )
            print(f"   ✅ Interface verified: {spec.module_name}")
        except LintError as e:
            print(f"❌ Interface check failed — fix spec errors first:\n{e.message}",
                  file=sys.stderr)
            return 1

        if not spec.todo_function_names:
            print(f"   ✅ No TODOs — spec is complete.")
            return 0

        print(f"   TODO functions: {', '.join(spec.todo_function_names)}")

        # ── Step 2: Fill TODOs via LLM ──
        print(f"🤖 Step 2: Filling TODOs via {args.child}...")
        try:
            candidates = fill_todos(
                spec=spec,
                child=args.child,
                max_retries=args.retries,
                timeout=args.timeout,
                verbose=True,
            )
        except FillError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1

        # ── Step 3: Verify agent implementations against spec ──
        # Generate a complete .fst with spec types + val sigs + agent's code
        # Then run F* on it WITHOUT --admit_smt_queries to ensure the
        # implementations actually satisfy the REQUIRES/ENSURES contracts
        print(f"🔧 Step 3: Verifying agent implementations against spec contracts...")
        filled_dir = args.out_dir / "filled"
        filled_dir.mkdir(parents=True, exist_ok=True)

        from .spec import generate_complete_fst
        from .lint import find_fstar

        # Combine all candidate code into one implementation string
        all_impl_code = ""
        for idx in sorted(candidates.keys()):
            all_impl_code += candidates[idx] + "\n"

        module_name = args.module_name or spec.module_name

        # Generate the complete .fst with spec + agent implementations
        filled_fst = generate_complete_fst(spec, all_impl_code)
        fst_path = filled_dir / f"{module_name}.fst"
        fst_path.write_text(filled_fst)

        # Verify: run F* on the .fst WITHOUT admits
        fstar = find_fstar()
        if not fstar:
            print("   fstar.exe not found — skipping verification")
            return 1

        with tempfile.TemporaryDirectory(prefix="veri-verify-") as verify_tmp:
            verify_fst = Path(verify_tmp) / f"{module_name}.fst"
            verify_fst.write_text(filled_fst)

            result = subprocess.run(
                [fstar, "--z3rlimit", str(args.rlimit * 2),
                 "--fuel", str(args.fuel * 2), "--ifuel", str(args.fuel * 2),
                 str(verify_fst)],
                capture_output=True, text=True, timeout=60,
            )

            if result.returncode != 0:
                print(f"   ⚠️  Implementation verification failed:")
                for line in (result.stderr + result.stdout).split('\n'):
                    if 'Error' in line or 'error' in line:
                        print(f"     ❌ {line}")
                print(f"   \n   Implementation saved to {fst_path} for manual fix.")
                return 1

        print(f"   ✅ Agent implementations satisfy spec contracts!")

        # ── Step 4: Compile verified code to C/Rust (if target specified) ──
        if args.target != "none":
            compile_target(
                args.spec,
                module_name=module_name,
                target=args.target,
                out_dir=args.out_dir,
                fstar_bin=fstar,
                impl_code=all_impl_code,
            )

        print(f"\n✅ Done. Verified .fst: {fst_path}")
        if args.target != "none":
            print(f"   Compiled artifacts in: {args.out_dir}")
        return 0

    except (SyntaxError, ValueError, FileNotFoundError) as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1


def compile_target(
    veri_path: Path,
    module_name: str,
    target: str,
    out_dir: Path,
    fstar_bin: Optional[str] = None,
    impl_code: str = "",
):
    """Compile a verified .veri.md to WASM or .so.

    Uses the verified implementation code (from agent) to compile,
    not regenerated admit() stubs. This ensures the compiled artifact
    actually matches the spec's contracts.

    Args:
        veri_path: Path to .veri.md
        module_name: Module name for F* extraction
        target: Compilation target ("wasm", "so", "none")
        out_dir: Output directory
        fstar_bin: Path to fstar.exe
        impl_code: F* let-bindings from agent (the verified implementation).
                   If empty, a warning is printed and extraction proceeds
                   with admit() stubs as fallback.
    """
    from .compile import (
        extract_c, compile_wasm, generate_wrappers,
    )

    if not impl_code:
        print("   ⚠️ No implementation code provided — compiling with admit() stubs."
              " The extracted C will NOT implement the spec correctly.")

    print(f"🔧 Extracting C via KaRaMeL (using {'agent code' if impl_code else 'admit() stubs'})...")
    c_path = extract_c(
        veri_path, module_name, fstar_bin=fstar_bin, impl_code=impl_code
    )
    if not c_path:
        print("   ⚠️ C extraction failed.")
        return

    print(f"   C source: {c_path}")

    if target == "wasm":
        print(f"🔧 Compiling to WASM...")
        wasm_path = compile_wasm(c_path, module_name, out_dir)
        if wasm_path:
            print(f"   ✅ WASM: {wasm_path}")

    print(f"📝 Generating wrappers...")
    ts_path, py_path = generate_wrappers(c_path, module_name, out_dir)
    if ts_path:
        print(f"   TS wrapper: {ts_path}")
    if py_path:
        print(f"   Py wrapper: {py_path}")


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point. Returns exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "lint":
        return cmd_lint(args)
    elif args.command == "compile":
        return cmd_compile(args)
    elif args.command == "verify":
        return cmd_verify(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
