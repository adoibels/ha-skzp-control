"""Testy eksportów pakietu używanych przez formularz i platformy."""
import ast
import importlib.util
import unittest
import support


class PackageImportTests(unittest.TestCase):
    def test_config_flow_package_imports(self):
        # Uruchom moduł wejściowy integracji, a nie tylko wybrane moduły.
        spec = importlib.util.spec_from_file_location(
            'skzp_control', support.ROOT / '__init__.py',
            submodule_search_locations=[str(support.ROOT)],
        )
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
        tree = ast.parse((support.ROOT / 'config_flow.py').read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
                for alias in node.names:
                    with self.subTest(imported_name=alias.name):
                        self.assertTrue(hasattr(package, alias.name),
                            f'config_flow imports missing package export: {alias.name}')
        self.assertFalse(hasattr(package, "SkzpTcpClient"))
        self.assertFalse(hasattr(package, 'format_communication_error'))
        self.assertIn('from .coordinator import format_communication_error', (support.ROOT / 'config_flow.py').read_text(encoding='utf-8-sig'))
