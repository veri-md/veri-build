"""
test_config — tests for veri_build.config (.veri/veri.toml project config).

Covers project-root discovery (walk-up + not-found), parsing a sample
veri.toml, ~ expansion and relative-path resolution, validation errors
(missing fields, duplicate names, missing spec), module lookup, and key
resolution. Pure host tests (no Docker, no Dafny).

Run directly:    python3 -m unittest tests/test_config.py
Or via pytest:   python3 -m pytest tests/test_config.py
"""

import tempfile
import unittest
from pathlib import Path

from veri_build.config import (
    ConfigError,
    find_project_root,
    load_config,
    lookup_module,
    resolve_key,
)


def _make_project(tmp: Path, toml_text: str, *, make_spec: bool = True) -> Path:
    """Create a project root with .veri/veri.toml (and optionally the spec)."""
    veri_dir = tmp / '.veri'
    veri_dir.mkdir(parents=True, exist_ok=True)
    (veri_dir / 'veri.toml').write_text(toml_text)
    if make_spec:
        spec = tmp / 'src' / 'trust_engine.veri.md'
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text('# spec\n')
    return tmp


SAMPLE = """\
[keys]
anthropic = "{key}"

[[modules]]
name    = "trust_engine"
spec    = "src/trust_engine.veri.md"
output  = "src/trust_engine/kernel/"
timeout = 1800
"""


class FindRootTests(unittest.TestCase):
    def test_walks_up_to_find_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), SAMPLE.format(key='~/.k'))
            nested = root / 'src' / 'deep' / 'nested'
            nested.mkdir(parents=True)
            self.assertEqual(find_project_root(nested), root.resolve())

    def test_finds_at_root_itself(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), SAMPLE.format(key='~/.k'))
            self.assertEqual(find_project_root(root), root.resolve())

    def test_not_found_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ConfigError):
                find_project_root(Path(d))


class LoadConfigTests(unittest.TestCase):
    def test_parses_and_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), SAMPLE.format(key='~/.veri-api-key'))
            cfg = load_config(root)
            self.assertEqual(cfg.root, root.resolve())
            self.assertEqual(cfg.keys['anthropic'], '~/.veri-api-key')
            self.assertEqual(len(cfg.modules), 1)
            mod = cfg.modules[0]
            self.assertEqual(mod.name, 'trust_engine')
            # spec/output resolved to absolute paths under root
            self.assertTrue(mod.spec.is_absolute())
            self.assertEqual(mod.spec, (root / 'src' / 'trust_engine.veri.md').resolve())
            self.assertEqual(mod.output, (root / 'src' / 'trust_engine' / 'kernel').resolve())
            self.assertEqual(mod.timeout, 1800)

    def test_missing_required_field(self):
        toml = '[[modules]]\nname = "x"\nspec = "src/trust_engine.veri.md"\n'  # no output
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), toml)
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_duplicate_module_name(self):
        toml = (
            '[[modules]]\nname="a"\nspec="src/trust_engine.veri.md"\noutput="o1"\n'
            '[[modules]]\nname="a"\nspec="src/trust_engine.veri.md"\noutput="o2"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), toml)
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_missing_spec_file(self):
        toml = '[[modules]]\nname="a"\nspec="src/does_not_exist.veri.md"\noutput="o"\n'
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), toml, make_spec=False)
            with self.assertRaises(ConfigError):
                load_config(root)

    def test_bad_timeout_type(self):
        toml = (
            '[[modules]]\nname="a"\nspec="src/trust_engine.veri.md"\n'
            'output="o"\ntimeout="soon"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), toml)
            with self.assertRaises(ConfigError):
                load_config(root)


class LookupAndKeyTests(unittest.TestCase):
    def test_lookup_module(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), SAMPLE.format(key='~/.k'))
            cfg = load_config(root)
            self.assertEqual(lookup_module(cfg, 'trust_engine').name, 'trust_engine')
            with self.assertRaises(ConfigError):
                lookup_module(cfg, 'nope')

    def test_resolve_key_reads_and_strips(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            key_file = root / 'mykey'
            key_file.write_text('sk-ant-secret\n')
            _make_project(root, SAMPLE.format(key=str(key_file)))
            cfg = load_config(root)
            self.assertEqual(resolve_key(cfg, 'anthropic'), 'sk-ant-secret')

    def test_resolve_key_missing_provider_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), SAMPLE.format(key='~/.k'))
            cfg = load_config(root)
            self.assertIsNone(resolve_key(cfg, 'openai'))

    def test_resolve_key_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            root = _make_project(Path(d), SAMPLE.format(key=str(Path(d) / 'absent')))
            cfg = load_config(root)
            with self.assertRaises(ConfigError):
                resolve_key(cfg, 'anthropic')


if __name__ == '__main__':
    unittest.main()
