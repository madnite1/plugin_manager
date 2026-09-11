import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def load_plugin_manager_module():
    plugins_mod = types.ModuleType("plugins")
    metadata_mod = types.ModuleType("plugins.metadata")
    base_mod = types.ModuleType("plugins.metadata.base")

    class BaseMetadataProvider:
        def __init__(self):
            pass

    base_mod.BaseMetadataProvider = BaseMetadataProvider
    sys.modules.setdefault("plugins", plugins_mod)
    sys.modules.setdefault("plugins.metadata", metadata_mod)
    sys.modules["plugins.metadata.base"] = base_mod

    module_path = Path(__file__).resolve().parents[1] / "plugin_manager.py"
    spec = importlib.util.spec_from_file_location("plugin_manager_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin_manager.py를 로드할 수 없습니다.")
    spec.loader.exec_module(module)
    return module


def write_latest_contract_provider(
    root: Path,
    *,
    sessions_expr="'all'",
    include_data_methods=True,
    create_detail_files=True,
    include_all_detail_manifest=True,
):
    root.mkdir(parents=True, exist_ok=True)
    detail_manifest = ["detail/index.html", "detail/style.css", "detail/script.js"]
    if not include_all_detail_manifest:
        detail_manifest = ["detail/index.html", "detail/style.css"]
    files = ["demo.py", "__init__.py", "VERSION", *detail_manifest]
    methods = ""
    if include_data_methods:
        methods = (
            "    def get_dashboard_data(self, db_type, limit=10): return {'success': True, 'items': []}\n"
            "    def get_detail_sidebar_data(self, db_type, context): return {'success': True, 'items': []}\n"
        )
    (root / "demo.py").write_text(
        "from plugins.metadata.base import BaseMetadataProvider\n"
        "SESSIONS = load_sessions()\n"
        "class DemoProvider(BaseMetadataProvider):\n"
        "    id = 'demo'\n"
        "    name = 'Demo'\n"
        "    is_searchable = False\n"
        "    config_schema = []\n"
        f"    home_widget = {{'title': 'Home', 'sessions': {sessions_expr}, 'layout': 'grid', 'size': 2}}\n"
        "    detail_sidebar_widget = {'title': 'Side', 'order': 10, 'sessions': ['video']}\n"
        "    detail_view = {'title': 'Detail', 'sessions': ['video']}\n"
        "    update_manifest = {\n"
        "        'enabled': True,\n"
        "        'provider': 'github-raw',\n"
        "        'raw_base_url': 'https://raw.githubusercontent.com/example/demo/main',\n"
        f"        'files': {files!r},\n"
        "        'version_file': 'VERSION',\n"
        "        'version_key': 'plugin version',\n"
        "    }\n"
        f"{methods}"
        "    def search(self, db_type, query): return []\n"
        "    def apply(self, db_type, book_id, item_data): return True, 'ok'\n",
        encoding="utf-8",
    )
    (root / "VERSION").write_text('{"plugin version": "1.0.0"}\n', encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")
    if create_detail_files:
        detail = root / "detail"
        detail.mkdir()
        (detail / "index.html").write_text("<main></main>", encoding="utf-8")
        (detail / "style.css").write_text("main{}", encoding="utf-8")
        (detail / "script.js").write_text("function render(){}", encoding="utf-8")


def write_provider(root: Path, *, detail_view=False, manifest_files=None):
    root.mkdir(parents=True, exist_ok=True)
    detail_decl = (
        '    detail_view = {"title": "Detail", "sessions": "all"}\n'
        if detail_view
        else ""
    )
    manifest_decl = ""
    if manifest_files is not None:
        manifest_decl = (
            "    update_manifest = {\n"
            "        'enabled': True,\n"
            "        'provider': 'github-raw',\n"
            "        'raw_base_url': 'https://raw.githubusercontent.com/example/demo/main',\n"
            f"        'files': {manifest_files!r},\n"
            "        'version_file': 'VERSION',\n"
            "        'version_key': 'plugin version',\n"
            "    }\n"
        )
    (root / "demo.py").write_text(
        "from plugins.metadata.base import BaseMetadataProvider\n\n"
        "class DemoProvider(BaseMetadataProvider):\n"
        "    id = 'demo'\n"
        "    name = 'Demo'\n"
        "    is_searchable = False\n"
        "    config_schema = []\n"
        f"{detail_decl}"
        f"{manifest_decl}"
        "    def search(self, db_type, query):\n"
        "        return []\n"
        "    def apply(self, db_type, book_id, item_data):\n"
        "        return True, 'ok'\n",
        encoding="utf-8",
    )
    (root / "VERSION").write_text('{"plugin version": "1.0.0"}\n', encoding="utf-8")
    (root / "__init__.py").write_text("", encoding="utf-8")


class PluginManagerPhase0RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pm_module = load_plugin_manager_module()

    def setUp(self):
        self.manager = self.pm_module.PluginManagerMetadataProvider.__new__(
            self.pm_module.PluginManagerMetadataProvider
        )
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_post_install_static_verification_stays_minimal(self):
        """사후 검증은 UI 계약과 무관하게 Provider id/VERSION 최소 무결성만 본다."""
        plugin_dir = self.root / "demo"
        write_provider(plugin_dir, detail_view=True)

        ok, error = self.manager._verify_installed_plugin_static(
            str(plugin_dir), "demo", expected_version="1.0.0"
        )

        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_update_manifest_extraction_keeps_exact_file_list(self):
        """update_manifest.files를 암묵적으로 확장하지 않는다."""
        plugin_dir = self.root / "demo"
        files = ["demo.py", "VERSION", "detail/index.html"]
        write_provider(plugin_dir, manifest_files=files)

        extracted, manifest = self.manager._extract_update_manifest_files(str(plugin_dir))

        self.assertEqual(extracted, files)
        self.assertEqual(manifest["files"], files)

    def test_prune_keeps_only_explicit_managed_files(self):
        """관리 목록에 없는 detail 자산은 자동 보존하지 않는다."""
        plugin_dir = self.root / "demo"
        detail_dir = plugin_dir / "detail"
        detail_dir.mkdir(parents=True)
        (plugin_dir / "demo.py").write_text("x = 1\n", encoding="utf-8")
        (detail_dir / "index.html").write_text("<main></main>", encoding="utf-8")
        (detail_dir / "script.js").write_text("console.log('x')", encoding="utf-8")
        (detail_dir / "style.css").write_text("body{}", encoding="utf-8")

        self.manager._prune_plugin_dir(
            str(plugin_dir), ["demo.py", "detail/index.html"]
        )

        self.assertTrue((plugin_dir / "demo.py").is_file())
        self.assertTrue((detail_dir / "index.html").is_file())
        self.assertFalse((detail_dir / "script.js").exists())
        self.assertFalse((detail_dir / "style.css").exists())

    def test_build_update_spec_does_not_infer_ui_files(self):
        manifest = {
            "enabled": True,
            "provider": "github-raw",
            "raw_base_url": "https://raw.githubusercontent.com/example/demo/main",
            "files": ["demo.py", "VERSION"],
            "version_file": "VERSION",
            "version_key": "plugin version",
        }

        spec = self.manager._build_update_spec("demo", manifest)

        self.assertEqual(spec["files"], ["demo.py", "VERSION"])

    def test_latest_contract_metadata_and_methods_are_discovered(self):
        plugin_dir = self.root / "demo"
        plugin_dir.mkdir()
        (plugin_dir / "demo.py").write_text(
            "from plugins.metadata.base import BaseMetadataProvider\n"
            "class DemoProvider(BaseMetadataProvider):\n"
            "    id = 'demo'\n"
            "    name = 'Demo'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
            "    home_widget = {'title': 'Home', 'sessions': 'all', 'layout': 'grid', 'size': 2}\n"
            "    detail_sidebar_widget = {'title': 'Side', 'order': 10, 'sessions': ['video']}\n"
            "    detail_view = {'title': 'Detail', 'sessions': ['video']}\n"
            "    def get_dashboard_data(self, db_type, limit=10): return {'success': True, 'items': []}\n"
            "    def get_detail_sidebar_data(self, db_type, context): return {'success': True, 'items': []}\n"
            "    def search(self, db_type, query): return []\n"
            "    def apply(self, db_type, book_id, item_data): return True, 'ok'\n",
            encoding="utf-8",
        )

        meta = self.manager._extract_provider_metadata(str(plugin_dir))

        self.assertTrue(self.manager._provider_capability(meta, "home_widget"))
        self.assertTrue(self.manager._provider_capability(meta, "detail_sidebar_widget"))
        self.assertTrue(self.manager._provider_capability(meta, "detail_view"))
        self.assertIn("get_dashboard_data", meta["_methods"])
        self.assertIn("get_detail_sidebar_data", meta["_methods"])

    def test_dynamic_contract_field_is_partial_without_losing_capability(self):
        plugin_dir = self.root / "demo"
        plugin_dir.mkdir()
        (plugin_dir / "demo.py").write_text(
            "from plugins.metadata.base import BaseMetadataProvider\n"
            "SESSIONS = load_sessions()\n"
            "class DemoProvider(BaseMetadataProvider):\n"
            "    id = 'demo'\n"
            "    name = 'Demo'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
            "    detail_view = {'title': 'Detail', 'sessions': SESSIONS}\n"
            "    def search(self, db_type, query): return []\n"
            "    def apply(self, db_type, book_id, item_data): return True, 'ok'\n",
            encoding="utf-8",
        )

        meta = self.manager._extract_provider_metadata(str(plugin_dir))

        self.assertEqual(meta["detail_view"], {"title": "Detail"})
        self.assertEqual(meta["_contract_status"]["detail_view"]["status"], "partial")
        self.assertEqual(meta["_contract_status"]["detail_view"]["unresolved_fields"], ["sessions"])
        self.assertTrue(self.manager._provider_capability(meta, "detail_view"))

    def test_fully_dynamic_contract_is_unknown_not_truthy(self):
        plugin_dir = self.root / "demo"
        plugin_dir.mkdir()
        (plugin_dir / "demo.py").write_text(
            "from plugins.metadata.base import BaseMetadataProvider\n"
            "class DemoProvider(BaseMetadataProvider):\n"
            "    id = 'demo'\n"
            "    name = 'Demo'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
            "    detail_view = build_detail_view()\n"
            "    def search(self, db_type, query): return []\n"
            "    def apply(self, db_type, book_id, item_data): return True, 'ok'\n",
            encoding="utf-8",
        )

        meta = self.manager._extract_provider_metadata(str(plugin_dir))

        self.assertEqual(meta["_contract_status"]["detail_view"]["status"], "unresolved")
        self.assertIsNone(self.manager._provider_capability(meta, "detail_view"))

    def test_latest_optional_contracts_validate_when_complete(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir)

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertTrue(ok, checks)
        by_name = {item["name"]: item for item in checks}
        self.assertTrue(by_name["home_widget"]["ok"])
        self.assertTrue(by_name["detail_sidebar_widget"]["ok"])
        self.assertTrue(by_name["detail_view"]["ok"])

    def test_invalid_literal_sessions_is_blocking(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir, sessions_expr="'general'")

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertFalse(ok)
        home_check = next(item for item in checks if item["name"] == "home_widget")
        self.assertFalse(home_check["ok"])
        self.assertIn("sessions", home_check["detail"])

    def test_dynamic_sessions_is_warning_not_blocking(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir, sessions_expr="SESSIONS")

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertTrue(ok, checks)
        home_check = next(item for item in checks if item["name"] == "home_widget")
        self.assertTrue(home_check["ok"])
        self.assertTrue(home_check.get("warn"))

    def test_detail_view_requires_all_three_files(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir, create_detail_files=False)

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertFalse(ok)
        detail_check = next(item for item in checks if item["name"] == "detail_view")
        self.assertIn("필수 detail UI 파일 없음", detail_check["detail"])

    def test_detail_view_requires_exact_manifest_entries(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir, include_all_detail_manifest=False)

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertFalse(ok)
        detail_check = next(item for item in checks if item["name"] == "detail_view")
        self.assertIn("detail/script.js", detail_check["detail"])

    def test_missing_optional_data_methods_warn_only(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir, include_data_methods=False)

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertTrue(ok, checks)
        warnings = [item for item in checks if item.get("warn")]
        names = {item["name"] for item in warnings}
        self.assertIn("home_widget 데이터 메서드", names)
        self.assertIn("detail_sidebar_widget 데이터 메서드", names)

    def test_plugin_list_exposes_capability_tristate(self):
        plugins_root = self.root / "plugins"
        plugin_dir = plugins_root / "demo"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "demo.py").write_text(
            "from plugins.metadata.base import BaseMetadataProvider\n"
            "class DemoProvider(BaseMetadataProvider):\n"
            "    id = 'demo'\n"
            "    name = 'Demo'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
            "    home_widget = {'title': 'Home'}\n"
            "    detail_sidebar_widget = None\n"
            "    detail_view = build_detail_view()\n"
            "    def search(self, db_type, query): return []\n"
            "    def apply(self, db_type, book_id, item_data): return True, 'ok'\n",
            encoding="utf-8",
        )
        (plugin_dir / "VERSION").write_text('{"plugin version": "1.0.0"}\n', encoding="utf-8")

        meta = self.manager._extract_provider_metadata(str(plugin_dir))

        class Gateway:
            @staticmethod
            def get_setting(key, default=None):
                return default

        self.manager._get_plugins_base_dir = lambda: str(plugins_root)
        self.manager._discover_provider_map = lambda refresh=False: {"demo": meta}
        self.manager.get_db_gateway = lambda db_type: Gateway()
        self.manager._catalog_get_rollback_enabled = lambda db_type: False
        self.manager._catalog_get_update_path_selection_enabled = lambda db_type: False
        self.manager._read_git_source_info = lambda plugin_id: None
        self.manager._get_plugin_data_dir = lambda plugin_id: str(self.root / "data" / plugin_id)

        cards = self.manager._list_plugins("general")

        self.assertEqual(len(cards), 1)
        self.assertIs(cards[0]["supports_home_widget"], True)
        self.assertIs(cards[0]["supports_detail_sidebar_widget"], False)
        self.assertIsNone(cards[0]["supports_detail_view"])


if __name__ == "__main__":
    unittest.main()
