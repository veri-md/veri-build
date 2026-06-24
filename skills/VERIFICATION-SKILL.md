---
name: veri-dsl-verification
description: Write F*/Dafny specs, verify with fstar.exe/dafny inside Docker, and convert to user-facing Veri DSL. Use when the task involves formal verification, writing .veri.f.md/.veri.dfy.md files, running target verifiers, contract specifications, or compiling verified code to C (via Low*/KaRaMeL), OCaml, WASM, Java, JavaScript, Python, or Rust. Triggers on: "verify", "Veri DSL", "veri.md", "F*", "Dafny", "Python", "formally verified", "contract spec", "Low*", "target: string".
---

# Veri DSL Verification

## Docker: where verification happens

All verification (fstar.exe, dafny) runs inside the `verification-builder` Docker
container. The host machine has the veri-build Python package installed, but the
formal verifier binaries only exist inside the container.

**Container image**: `verification-builder:latest`
(Build with `docker build -t verification-builder:latest .` from `veri-build/`)

To run a command inside the container, mount your spec and use `python3 -m`:

```bash
docker run --rm -v $(pwd):/workspace verification-builder \
    "python3 -m veri_build.cli lint /workspace/spec.veri.md"
```

The container has `PYTHONPATH` preconfigured so the `veri_build` package is
importable directly.

## Core loop

Work in F* or Dafny. Write F*/Dafny in a temp `.veri.f.md` / `.veri.dfy.md`
file, then call `verify_and_convert()` to check and convert to user-facing
Veri DSL.

```
Write F*/Dafny  →  verify_and_convert()  →  valid Veri DSL or error
```

## Target declaration

Every `.veri.md` must declare its **TARGET** and **VERI_VERSION** in the first
```veri block. Example:

```veri
TARGET fstar-c
VERI_VERSION 0.0.2
```

- `VERI_VERSION` is **required** — lint errors if missing
- Version check compares **major.minor** only (`0.0.1` and `0.0.2` are compatible;
  `0.1.0` is not)
- See `CANONICAL_TARGETS` in `veri_build/pipeline.py` for all registered targets

## Pure discipline (all backends)

All Veri DSL specs must be pure — this constraint applies regardless of target:

- Functions are **pure** (`Pure`/`Tot`/`Lemma` in F*, `function`/`predicate`
  in Dafny, no side effects in Python). No `ST`, `Dv`, `ML`, `Exn` effects.
- Types are **pure** — no `HyperStack`, `Heap`, `ST` modules. No mutable
  references, no heap objects, no ST regions.

### F* / Low* specifics

When target is `fstar-c`, the pipeline additionally enforces the Low* subset:
- Low* subset only (required for KaRaMeL C compilation)
- No `ST` effect, no `HyperStack`/`Heap`/`ST` modules
- No `All`, `ML`, `Dv`, `Exn` effects

### Dafny specifics

Dafny targets (`dafny-java`, `dafny-js`, `dafny-python`, `dafny-rust`) enforce:
- Dafny `function` / `predicate` / `lemma` (no `method` with side effects)
- No `:=` mutations, no `array` mutation, no `new`
- All types must be compilable to the target language

## Primary API — verify_and_convert

```python
from veri_build.pipeline import verify_and_convert

# For F*-backed specs:
result = verify_and_convert(fstar_code, target='fstar', module_name='MyModule')

# For Dafny-backed specs:
result = verify_and_convert(dafny_code, target='dafny', module_name='MyModule')
```

VerifyConvertResult:
- `result.verified: bool` — did verification pass?
- `result.veri: str | None` — Veri DSL converted from verified code
- `result.error: str | None` — what went wrong

Note: `verify_and_convert` runs the verifier locally — it requires `fstar.exe`
or `dafny` on the PATH. Use the Docker-based lint for verification if those
aren't installed on the host.

## Secondary API — compile (Docker)

When the user has finished editing a Veri DSL spec and wants to compile it,
**always use `compile_veri` in a spawned sub-agent** — the process involves
Docker image builds, LLM agent calls, and verifier runs that can take several
minutes.

```python
# In a sub-agent, run:
from veri_build.pipeline import compile_veri, CompilerConfig

result = compile_veri("spec.veri.md", CompilerConfig(
    agent='claude', use_docker=True,
))
```

**Why sub-agent**: `compile_veri` with `use_docker=True` mounts the spec into
the container, launches an agent inside to fill `# TODO` blocks, runs the target
verifier, then compiles. This is a long-running isolated task — don't block the
main session.

**Output directory**: Compiled artifacts land in a `build/` directory next to the
spec.

## HARD RULE: Lint before delivering (inside Docker)

**Never present a `.veri.md` file to the user** — whether written, generated, or
converted — unless it passes lint. The lint check must be the last step before
showing the file.

Run lint inside the Docker container so that fstar.exe/dafny is available:

```bash
docker run --rm -v $(pwd):/workspace verification-builder \
    "python3 -m veri_build.cli lint /workspace/path/to/your.veri.md"
```

Expected output:
```
✅ path/to/your.veri.md: lint passed (<target>)
```

If lint reports any error, **do not show the file to the user**. Fix every
parse error first, then re-lint, and only deliver when it passes. A `.veri.md`
that doesn't lint is broken — it cannot be compiled, verified, or used in any
pipeline downstream step.

## User-facing Veri DSL format rules

After lint passes, deliver the `.veri.md`. A `.veri.md` file is a **markdown document**,
not a bare DSL file. Write your specification in natural language prose, and
place Veri DSL inside ` ```veri ` fenced code blocks. The first Veri DSL block
must declare the **TARGET** and **VERI_VERSION**:

````markdown
# Sorted List Specification

Target: F* → C via Low*/KaRaMeL

```veri
TARGET fstar-c
VERI_VERSION 0.0.2
```

## Element type

Each element has a numeric serial and a string data field.

```veri
class Element:
    serial: nat
    data: string
```
````

After the target declaration, add types, predicates, and function specs in any
order — each inside its own ` ```veri ` code block, with natural language
documentation surrounding it.

## File conventions

| File | Writer | Format | Purpose |
|------|--------|--------|---------|
| `spec.veri.md` | Pipeline | Veri DSL | User-facing — starts with `TARGET ...` + `VERI_VERSION` |
| `spec.veri.f.md` | **You** (LLM, temp) | F* | Working file for F* target |
| `spec.veri.dfy.md` | **You** (LLM, temp) | Dafny | Working file for Dafny target |

## Example (F* target)

```python
from veri_build.pipeline import verify_and_convert

# You wrote this in temp spec.veri.f.md — must be pure / Low* for C:
fstar = """
module SortedList

type element = {
    serial: Prims.nat;
    data: Prims.string;
}

let rec is_sorted (lst: list element) : Prims.bool =
    match lst with
    | [] -> true
    | _ :: [] -> true
    | hd1 :: hd2 :: tl -> hd1.serial <= hd2.serial && is_sorted (hd2 :: tl)

type valid_sorted_list = lst:list element{is_sorted lst}

val add_element:
  existing: valid_sorted_list ->
  new_elem: element ->
  Pure valid_sorted_list
    (requires True)
    (ensures (fun result ->
      is_sorted result /\
      List.Tot.length result = List.Tot.length existing + 1))
"""

# Verify + convert to Veri DSL
result = verify_and_convert(fstar, target='fstar', module_name='SortedList')

if result.verified:
    with open("sorted_list.veri.md", "w") as f:
        f.write(result.veri)
else:
    pass

# After writing, verify inside Docker:
#   docker run --rm -v $(pwd):/workspace verification-builder \
#       "python3 -m veri_build.cli lint /workspace/sorted_list.veri.md"
```

## Reference

- **Veri DSL syntax**: `veri-build/src/veri_build/dsl/README.md`
- **Pipeline API**: `veri-build/docs/API.md`
- **Examples**: `veri-build/examples/`
