"""Fill — Hook 2: Fill #TODO functions via LLM subprocess.

Takes a .veri.md spec, constructs a minimal prompt, calls the LLM,
extracts the F* let-binding, and returns it. No filesystem access
for the LLM — just stdin/stdout."""

import re
import subprocess
import shutil
from pathlib import Path
from typing import Optional, List, Tuple

from .spec import ExtractedSpec, spec_to_prompt


class FillError(Exception):
    """LLM failed to produce a valid F* candidate."""
    pass


def _find_cli(name: str) -> Optional[str]:
    """Locate an LLM CLI tool."""
    path = shutil.which(name)
    if path:
        return path
    # Check common global npm locations
    candidates = [
        f"~/.npm-global/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/opt/homebrew/bin/{name}",
    ]
    for c in candidates:
        p = Path(c).expanduser()
        if p.exists():
            return str(p)
    return None


def _extract_fstar(output: str) -> str:
    """Extract F* code from LLM output.

    Handles:
    - Raw let binding (no formatting)
    - Fenced code blocks (```fstar ... ``` or ``` ... ```)
    - Leading/trailing explanation text
    """
    text = output.strip()

    # Try code blocks first
    for pat in [r'```(?:fstar|fst|ocaml)?\n(.*?)```', r'```\n(.*?)```']:
        m = re.search(pat, text, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            if candidate.startswith('let '):
                return candidate

    # Look for let binding directly
    m = re.search(r'^(let\s+(?:rec\s+)?[a-zA-Z_]\w*\s.*?)(?:\n\n|\Z)', text, re.DOTALL | re.MULTILINE)
    if m:
        return m.group(1).strip()

    # Last resort: find any line starting with 'let '
    for line in text.split('\n'):
        if line.strip().startswith('let '):
            return line.strip()

    raise FillError(
        "LLM output did not contain a valid F* let binding.\n"
        f"Expected 'let fn_name ... = ...'\n"
        f"Got:\n{text[:500]}"
    )


def _resolve_model(default: str) -> str:
    """Resolve model from ANTHROPIC_MODEL env var, falling back to default."""
    import os
    return os.environ.get("ANTHROPIC_MODEL", default)


def call_claude(
    prompt: str,
    claude_bin: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """Call Claude Code CLI subprocess to fill a TODO.

    Args:
        prompt: The prompt containing the spec
        claude_bin: Path to claude binary (auto-detect if None)
        model: Claude model name (default: ANTHROPIC_MODEL env or claude-sonnet-4-5)
        timeout: Subprocess timeout in seconds

    Returns:
        F* let binding text
    """
    claude = claude_bin or _find_cli("claude")
    if not claude:
        raise FillError(
            "claude not found on PATH. Install it or use --child pi."
        )

    resolved_model = model or _resolve_model("claude-sonnet-4-5")

    result = subprocess.run(
        [claude, "-p", prompt, "--print", "--model", resolved_model],
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise FillError(
            f"claude exited with code {result.returncode}:\n{result.stderr[:500]}"
        )

    return _extract_fstar(result.stdout)


def call_pi(
    prompt: str,
    pi_bin: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """Call OpenClaw pi-coding-agent CLI subprocess to fill a TODO.

    Args:
        prompt: The prompt containing the spec
        pi_bin: Path to pi binary (auto-detect if None)
        model: Model name for pi to use (default: ANTHROPIC_MODEL env or deepseek/deepseek-v4-pro)
        timeout: Subprocess timeout in seconds

    Returns:
        F* let binding text
    """
    pi = pi_bin or _find_cli("pi")
    if not pi:
        raise FillError(
            "pi not found on PATH. Install it or use --child claude."
        )

    resolved_model = model or _resolve_model("deepseek/deepseek-v4-pro")

    result = subprocess.run(
        [pi, "--model", resolved_model, prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise FillError(
            f"pi exited with code {result.returncode}:\n{result.stderr[:500]}"
        )

    return _extract_fstar(result.stdout)


def fill_todos(
    spec: ExtractedSpec,
    child: str = "claude",
    max_retries: int = 3,
    timeout: int = 30,
    verbose: bool = True,
) -> dict:
    """Fill all #TODO functions in the spec using an LLM.

    For each TODO block, calls the LLM with the spec, extracts F*,
    and retries up to max_retries times.

    Args:
        spec: Parsed spec with TODOs
        child: Which LLM CLI to use ("claude" or "pi")
        max_retries: Max attempts per TODO block
        timeout: Per-call timeout in seconds
        verbose: Print progress to stderr

    Returns:
        Mapping from block index → F* candidate text

    Raises:
        FillError: If all retries fail
    """
    prompt = spec_to_prompt(spec)
    caller = call_claude if child == "claude" else call_pi

    candidates = {}
    remaining_indices = list(spec.todo_indices)

    if not remaining_indices:
        return candidates

    if verbose:
        print(f"  [fill] TODO functions: {', '.join(spec.todo_function_names)}", flush=True)
        print(f"  [fill] Calling {child}...", flush=True)

    for attempt in range(max_retries):
        try:
            fstar_code = caller(prompt, timeout=timeout)
        except (FillError, subprocess.TimeoutExpired) as e:
            if verbose:
                print(f"  [fill] Attempt {attempt + 1} failed: {e}", flush=True)
            if attempt < max_retries - 1:
                prompt += f"\n\nPrevious attempt failed. Try again. Return ONLY the let binding."
                continue
            raise FillError(
                f"Failed to fill TODOs after {max_retries} attempts."
            ) from e

        # Validate: must be valid F* (starts with let)
        if not fstar_code.strip().startswith("let "):
            if verbose:
                print(f"  [fill] Attempt {attempt + 1}: output didn't start with 'let'", flush=True)
            prompt += f"\n\nYour response must start with 'let ' and be valid F*. Try again."
            continue

        # One candidate for one block (for now — single TODO per pass)
        if remaining_indices:
            candidates[remaining_indices[0]] = fstar_code

        if verbose:
            print(f"  [fill] ✅ Candidate received ({len(fstar_code)} chars)", flush=True)

        return candidates

    raise FillError(f"Failed to fill TODOs after {max_retries} attempts.")
