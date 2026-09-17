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

    def test_update_manifest_status_distinguishes_missing_disabled_invalid_and_enabled(self):
        valid = {
            "enabled": True,
            "provider": "github-raw",
            "raw_base_url": "https://raw.githubusercontent.com/example/demo/main",
            "files": ["demo.py", "VERSION"],
            "version_file": "VERSION",
            "version_key": "plugin version",
        }

        self.assertEqual(self.manager._classify_update_manifest("demo", None), "missing")
        self.assertEqual(
            self.manager._classify_update_manifest("demo", {**valid, "enabled": False}),
            "disabled",
        )
        self.assertEqual(
            self.manager._classify_update_manifest("demo", {"enabled": True}),
            "invalid",
        )
        self.assertEqual(
            self.manager._classify_update_manifest("demo", valid, is_monorepo_subdir=True),
            "enabled",
        )
        self.assertEqual(self.manager._classify_update_manifest("demo", valid), "enabled")

    def test_monorepo_subdir_manifest_can_check_remote_version(self):
        manifest = {
            "enabled": True,
            "provider": "github-raw",
            "raw_base_url": (
                "https://raw.githubusercontent.com/example/mono/main/"
                "plugins/metadata/demo"
            ),
            "files": ["demo.py", "VERSION"],
            "version_file": "VERSION",
            "version_key": "plugin version",
        }
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/example/mono",
            "branch": "main",
        }
        self.manager._resolve_update_ref = lambda *args, **kwargs: {
            "mode": "branch",
            "ref_name": "main",
            "raw_base_url": (
                "https://raw.githubusercontent.com/example/mono/main/"
                "plugins/metadata/demo"
            ),
            "parsed": {
                "type": "github",
                "host": "github.com",
                "owner": "example",
                "repo": "mono",
            },
        }
        self.manager._fetch_remote_plugin_version = lambda *args, **kwargs: "2.0.0"

        has_update, latest, status = self.manager._check_plugin_update_detail(
            "demo",
            "1.0.0",
            {"update_manifest": manifest},
            "general",
        )

        self.assertTrue(has_update)
        self.assertEqual(latest, "2.0.0")
        self.assertEqual(status, "ok")

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

    def test_detail_view_does_not_depend_on_manifest_file_entries(self):
        plugin_dir = self.root / "demo"
        write_latest_contract_provider(plugin_dir, include_all_detail_manifest=False)

        ok, checks = self.manager._validate_plugin_source(str(plugin_dir), "demo")

        self.assertTrue(ok, checks)
        detail_check = next(item for item in checks if item["name"] == "detail_view")
        self.assertTrue(detail_check["ok"])

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


    def test_gitea_topic_search_does_not_fallback_to_keyword_query(self):
        server = {
            "url": "https://git.example.com",
            "host": "git.example.com",
            "token": "",
        }
        calls = []

        def fake_search(db_type, topic, source="github", gitea_server=None):
            calls.append({
                "topic": topic,
                "source": source,
                "server": gitea_server,
            })
            return {
                "data": [
                    {
                        "full_name": "bookoasis/demo",
                        "html_url": "https://git.example.com/bookoasis/demo",
                        "description": "BookOasis plugin without topic",
                        "topics": [],
                        "default_branch": "main",
                        "updated_at": "2026-09-17T00:00:00Z",
                    }
                ]
            }

        self.manager._catalog_search_topic = fake_search

        result = self.manager._catalog_search_gitea_topic(
            "general", "bookoasis-plugin", server
        )

        self.assertEqual(result, {"items": []})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["topic"], "bookoasis-plugin")
        self.assertEqual(calls[0]["source"], "gitea")

    def test_successful_gitea_refresh_removes_stale_repo_without_configured_topic(self):
        db_path = self.root / "plugin_manager.db"
        plugins_root = self.root / "plugins"
        plugins_root.mkdir()
        self.manager._get_catalog_db_path = lambda: str(db_path)
        self.manager._get_db_path = lambda: str(db_path)
        self.manager._get_plugins_base_dir = lambda: str(plugins_root)
        self.manager._catalog_init_db()
        self.manager._catalog_db_execute(
            """
            INSERT INTO repos(full_name, html_url, description, topics, default_branch, pushed_at,
                              plugin_id, plugin_name, latest_version, is_valid, last_checked,
                              install_error, source, base_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "bookoasis/stale",
                "https://git.example.com/bookoasis/stale",
                "stale repo",
                "[]",
                "main",
                "2026-09-16T00:00:00Z",
                "stale",
                "Stale",
                "1.0.0",
                "valid",
                "2026-09-16T00:01:00Z",
                None,
                "gitea",
                "https://git.example.com",
            ),
        )
        self.manager._catalog_get_topics = lambda db_type: ["bookoasis-plugin"]
        self.manager._catalog_search_topic = lambda *args, **kwargs: {"items": []}
        self.manager._catalog_get_gitea_servers = lambda db_type: [
            {
                "url": "https://git.example.com",
                "host": "git.example.com",
                "token": "",
                "enabled": True,
            }
        ]
        self.manager._catalog_search_gitea_topic = lambda *args, **kwargs: {"items": []}

        ok, _message = self.manager._catalog_refresh_once("general", force_verify=True)

        self.assertTrue(ok)
        self.assertEqual(
            self.manager._catalog_db_query(
                "SELECT full_name FROM repos WHERE full_name='bookoasis/stale'"
            ),
            [],
        )

    def test_catalog_manifest_requires_update_contract(self):
        valid = {
            "enabled": True,
            "provider": "github-raw",
            "raw_base_url": "https://raw.githubusercontent.com/example/demo/main",
            "files": ["demo.py", "__init__.py", "VERSION"],
            "version_file": "VERSION",
            "version_key": "plugin version",
        }
        self.assertTrue(self.manager._catalog_manifest_is_installable(valid, "demo.py"))

        missing = dict(valid)
        missing.pop("files")
        self.assertFalse(self.manager._catalog_manifest_is_installable(missing, "demo.py"))

        disabled = dict(valid)
        disabled["enabled"] = False
        self.assertFalse(self.manager._catalog_manifest_is_installable(disabled, "demo.py"))

    def test_catalog_repo_without_manifest_is_valid_when_version_and_provider_exist(self):
        self.manager._fetch_text = lambda *args, **kwargs: '{"plugin version": "1.2.3"}'
        self.manager._catalog_fetch_plugin_meta = lambda *args, **kwargs: (
            "demo", "Demo", False
        )

        status, plugin_id, version, name = self.manager._catalog_check_repo_version(
            "example/demo", "main"
        )

        self.assertEqual(status, "valid")
        self.assertEqual(plugin_id, "demo")
        self.assertEqual(version, "1.2.3")
        self.assertEqual(name, "Demo")

    def test_catalog_provider_discovery_does_not_require_repo_name_match(self):
        provider_source = '''
from plugins.metadata.base import BaseMetadataProvider

class M3UPlayerPlugin(BaseMetadataProvider):
    id = "bookoasis_m3u_player"
    name = "ALIVE 라이브 플레이어"
    is_searchable = False
    config_schema = []
    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/venushoon/bookoasis-m3u-player/main",
        "files": ["bookoasis_m3u_player.py", "__init__.py", "VERSION"],
        "version_file": "VERSION",
        "version_key": "plugin version",
    }
    def search(self, db_type, query): return []
    def apply(self, db_type, book_id, item_data): return True, "ok"
'''
        responses = {
            "https://raw.githubusercontent.com/venushoon/bookoasis-m3u-player/main/__init__.py":
                "from .bookoasis_m3u_player import M3UPlayerPlugin\n",
            "https://raw.githubusercontent.com/venushoon/bookoasis-m3u-player/main/bookoasis_m3u_player.py":
                provider_source,
        }
        requested = []

        def fake_fetch(url, *args, **kwargs):
            requested.append(url)
            return responses.get(url)

        self.manager._fetch_text = fake_fetch

        plugin_id, name, manifest_ok = self.manager._catalog_fetch_plugin_meta(
            "venushoon/bookoasis-m3u-player", "main", "bookoasis-m3u-player"
        )

        self.assertEqual(plugin_id, "bookoasis_m3u_player")
        self.assertEqual(name, "ALIVE 라이브 플레이어")
        self.assertTrue(manifest_ok)
        self.assertIn(
            "https://raw.githubusercontent.com/venushoon/bookoasis-m3u-player/main/bookoasis_m3u_player.py",
            requested,
        )

    def test_replace_git_forwards_force_without_requiring_manifest(self):
        plugin_dir = self.root / "plugins" / "demo"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "demo.py").write_text("x = 1\n", encoding="utf-8")
        work_dir = self.root / "work"
        work_dir.mkdir()
        target_url = "https://example.invalid/demo"
        captured = {}

        self.manager._get_plugins_base_dir = lambda: str(plugin_dir.parent)
        self.manager._validate_plugin_path = lambda plugin_id: (str(plugin_dir), None)
        self.manager._catalog_replace_candidates = lambda plugin_id, db_type=None: [
            {"git_url": target_url}
        ]
        self.manager._get_work_dir = lambda: str(work_dir)
        self.manager._read_git_source_info = lambda plugin_id: None

        def fake_install(git_url, db_type, force=False, backup_dir=None, require_manifest=False):
            captured.update({
                "git_url": git_url,
                "force": force,
                "backup_dir": backup_dir,
                "require_manifest": require_manifest,
            })
            return False, "blocked"

        self.manager._install_from_git = fake_install

        ok, _ = self.manager._replace_plugin("demo", target_url, "general", force=True)

        self.assertFalse(ok)
        self.assertTrue(captured["force"])
        self.assertFalse(captured["require_manifest"])
        self.assertEqual(captured["git_url"], target_url)

    def test_update_action_forwards_force(self):
        captured = {}

        def fake_update(plugin_id, db_type, force=False):
            captured.update({"plugin_id": plugin_id, "db_type": db_type, "force": force})
            return True, "ok"

        self.manager._update_plugin = fake_update

        ok, _ = self.manager.apply(
            "general", None, {"action": "update", "plugin_id": "demo", "force": True}
        )

        self.assertTrue(ok)
        self.assertEqual(captured["plugin_id"], "demo")
        self.assertEqual(captured["db_type"], "general")
        self.assertTrue(captured["force"])

    def test_repository_update_validation_failure_uses_risk_confirm_flow(self):
        installed = self.root / "installed" / "demo"
        target = self.root / "target" / "demo"
        write_provider(installed)
        write_provider(target)
        (target / "VERSION").write_text(
            '{"plugin version": "2.0.0"}\n', encoding="utf-8"
        )
        checks = [{
            "name": "금지 패턴",
            "ok": False,
            "detail": "mr_resolver.py: subprocess import 발견",
            "guide_ref": "가이드 §2.1 보안 제약",
        }]

        self.manager._validate_plugin_path = lambda plugin_id: (str(installed), None)
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/example/demo",
            "branch": "main",
        }
        self.manager._download_repository_zip = lambda *args, **kwargs: (
            {"zip_bytes": b"zip", "ref_type": "branch", "ref_name": "main"}, None
        )
        self.manager._extract_repository_zip = lambda *args, **kwargs: str(target)
        self.manager._validate_plugin_source = lambda *args, **kwargs: (False, checks)
        self.manager._catalog_get_allow_invalid_install = lambda db_type: True

        ok, message = self.manager._update_plugin("demo", "general")

        self.assertFalse(ok)
        self.assertIn("__VALIDATION_FAILED__", message)
        self.assertIn("subprocess import 발견", message)

    def test_repository_update_force_bypasses_general_validation_after_confirmation(self):
        installed = self.root / "installed" / "demo"
        target = self.root / "target" / "demo"
        write_provider(installed)
        write_provider(target)
        (target / "VERSION").write_text(
            '{"plugin version": "2.0.0"}\n', encoding="utf-8"
        )
        checks = [{
            "name": "금지 패턴",
            "ok": False,
            "detail": "mr_resolver.py: subprocess import 발견",
            "guide_ref": "가이드 §2.1 보안 제약",
        }]
        captured = {}

        self.manager._validate_plugin_path = lambda plugin_id: (str(installed), None)
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/example/demo",
            "branch": "main",
        }
        self.manager._download_repository_zip = lambda *args, **kwargs: (
            {"zip_bytes": b"zip", "ref_type": "branch", "ref_name": "main"}, None
        )
        self.manager._extract_repository_zip = lambda *args, **kwargs: str(target)
        self.manager._validate_plugin_source = lambda *args, **kwargs: (False, checks)

        def fake_update_existing(target_dir, dest_dir, plugin_id, source_checks, db_type, force=False):
            captured.update({
                "target_dir": target_dir,
                "dest_dir": dest_dir,
                "plugin_id": plugin_id,
                "source_checks": source_checks,
                "db_type": db_type,
                "force": force,
            })
            return True, "updated"

        self.manager._update_existing_from_zip = fake_update_existing
        self.manager._sources_set = lambda *args, **kwargs: None

        ok, message = self.manager._update_plugin("demo", "general", force=True)

        self.assertTrue(ok, message)
        self.assertTrue(captured["force"])
        self.assertEqual(captured["source_checks"], checks)

    def test_legacy_raw_update_receives_force_from_update(self):
        installed = self.root / "installed" / "demo"
        write_provider(installed)
        manifest = {
            "enabled": True,
            "provider": "github-raw",
            "raw_base_url": "https://raw.githubusercontent.com/example/demo/main",
            "files": ["demo.py", "VERSION"],
            "version_file": "VERSION",
            "version_key": "plugin version",
        }
        captured = {}

        self.manager._validate_plugin_path = lambda plugin_id: (str(installed), None)
        self.manager._read_git_source_info = lambda plugin_id: None
        self.manager._extract_update_manifest_files = lambda plugin_dir: (
            ["demo.py", "VERSION"], manifest
        )

        def fake_raw(plugin_id, db_type, force=False):
            captured.update({"plugin_id": plugin_id, "db_type": db_type, "force": force})
            return True, "raw updated"

        self.manager._update_plugin_raw_legacy = fake_raw

        ok, _ = self.manager._update_plugin("demo", "general", force=True)

        self.assertTrue(ok)
        self.assertTrue(captured["force"])

    def test_repository_url_normalization_treats_dot_git_and_trailing_slash_as_same(self):
        normalize = self.manager._normalize_repository_url

        self.assertEqual(
            normalize("https://git.example.com/bookoasis/demo.git/"),
            "https://git.example.com/bookoasis/demo",
        )
        self.assertEqual(
            normalize("https://git.example.com/bookoasis/demo/"),
            "https://git.example.com/bookoasis/demo",
        )

    def test_replaced_source_is_hidden_only_while_catalog_fingerprint_is_unchanged(self):
        db_path = self.root / "plugin_manager.db"
        self.manager._get_catalog_db_path = lambda: str(db_path)
        self.manager._get_db_path = lambda: str(db_path)
        self.manager._catalog_init_db()

        old_url = "https://git.example.com/legacy/demo.git"
        new_url = "https://git.example.com/new/demo"
        row = {
            "full_name": "legacy/demo",
            "html_url": "https://git.example.com/legacy/demo",
            "description": "old",
            "topics": "[]",
            "default_branch": "main",
            "pushed_at": "2026-09-16T01:00:00Z",
            "plugin_id": "demo",
            "plugin_name": "Demo",
            "latest_version": "1.0.0",
            "is_valid": "valid",
            "last_checked": "2026-09-16T01:01:00Z",
            "install_error": None,
            "source": "gitea",
            "base_url": "https://git.example.com",
        }
        self.manager._catalog_db_execute(
            """
            INSERT INTO repos(full_name, html_url, description, topics, default_branch, pushed_at,
                              plugin_id, plugin_name, latest_version, is_valid, last_checked,
                              install_error, source, base_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row[k] for k in (
                "full_name", "html_url", "description", "topics", "default_branch", "pushed_at",
                "plugin_id", "plugin_name", "latest_version", "is_valid", "last_checked",
                "install_error", "source", "base_url",
            )),
        )

        self.assertTrue(
            self.manager._catalog_record_replace_suppression("demo", old_url, new_url, row=row)
        )
        self.assertEqual(
            self.manager._catalog_db_query("SELECT * FROM repos WHERE full_name='legacy/demo'"), []
        )

        # 다음 카탈로그 갱신에서 같은 저장소 상태가 다시 수집되면 역방향 후보로 숨긴다.
        self.assertTrue(
            self.manager._catalog_candidate_is_suppressed(
                "demo", "https://git.example.com/legacy/demo/", row
            )
        )

        # 이후 실제 push가 발생하면 fingerprint가 달라져 다시 후보가 되고 억제 기록도 제거한다.
        changed = dict(row, pushed_at="2026-09-16T02:00:00Z")
        self.assertFalse(
            self.manager._catalog_candidate_is_suppressed("demo", old_url, changed)
        )
        self.assertEqual(
            self.manager._catalog_db_query(
                "SELECT * FROM replace_suppressions WHERE plugin_id='demo'"
            ),
            [],
        )

    def test_replace_candidates_excludes_current_repo_after_url_normalization(self):
        row = {
            "full_name": "bookoasis/demo",
            "html_url": "https://git.example.com/bookoasis/demo",
            "default_branch": "main",
            "pushed_at": "2026-09-16T01:00:00Z",
            "plugin_id": "demo",
            "plugin_name": "Demo",
            "latest_version": "1.0.0",
            "source": "gitea",
            "base_url": "https://git.example.com",
        }
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://git.example.com/bookoasis/demo.git/"
        }
        self.manager._catalog_list_valid_repos = lambda db_type=None: [row]
        self.manager._catalog_candidate_is_suppressed = lambda *args, **kwargs: False

        self.assertEqual(self.manager._catalog_replace_candidates("demo", "general"), [])

    def test_catalog_reverifies_when_repository_changed_after_last_check(self):
        now = self.pm_module.datetime(2026, 9, 13, 14, 30, tzinfo=self.pm_module.timezone.utc)
        row = {
            "last_checked": "2026-09-13T13:00:20Z",
            "pushed_at": "2026-09-13T23:12:20+09:00",
        }

        self.assertTrue(self.manager._catalog_repo_needs_verify(row, now=now))

    def test_catalog_keeps_recent_unchanged_repository_cached(self):
        now = self.pm_module.datetime(2026, 9, 13, 14, 30, tzinfo=self.pm_module.timezone.utc)
        row = {
            "last_checked": "2026-09-13T14:00:00Z",
            "pushed_at": "2026-09-13T13:30:00Z",
        }

        self.assertFalse(self.manager._catalog_repo_needs_verify(row, now=now))

    def test_catalog_reverifies_when_ttl_expires(self):
        now = self.pm_module.datetime(2026, 9, 14, 14, 30, tzinfo=self.pm_module.timezone.utc)
        row = {
            "last_checked": "2026-09-13T14:00:00Z",
            "pushed_at": "2026-09-13T13:30:00Z",
        }

        self.assertTrue(self.manager._catalog_repo_needs_verify(row, now=now))

    def test_check_update_always_returns_current_replace_candidates(self):
        plugins_root = self.root / "plugins"
        plugin_dir = plugins_root / "demo"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "VERSION").write_text(
            '{"plugin version": "1.0.0"}\n', encoding="utf-8"
        )
        candidate = {
            "git_url": "https://git.example.com/bookoasis/demo",
            "latest_version": "2.0.0",
        }

        self.manager._get_plugins_base_dir = lambda: str(plugins_root)
        self.manager._discover_provider_map = lambda refresh=False: {
            "demo": {"update_manifest": None}
        }
        self.manager._read_git_source_info = lambda plugin_id: None
        self.manager._check_plugin_update_detail = lambda *args, **kwargs: (
            False, "1.0.0", "no_manifest"
        )
        self.manager._catalog_replace_candidates = lambda plugin_id, db_type=None: [candidate]

        ok, result = self.manager._check_update_action("demo", "general")

        self.assertTrue(ok)
        self.assertEqual(result["replace_candidates"], [candidate])
        self.assertFalse(result.get("update_blocked", False))

    def test_installed_git_source_overrides_stale_manifest_repository(self):
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/betty2859/media_tool",
            "branch": "main",
            "update_channel": "branch",
            "update_channel_explicit": 1,
        }
        self.manager._catalog_get_update_path_selection_enabled = lambda db_type: True
        self.manager._catalog_default_branch_for_repo = lambda parsed: "main"
        self.manager._fetch_repository_default_branch = lambda parsed, db_type=None: None

        resolved = self.manager._resolve_update_ref(
            "media_tool",
            "https://gitea.derekkoo.win/bookoasis/media-tool/raw/branch/main",
            ["media_tool.py", "VERSION"],
            "general",
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["parsed"]["type"], "github")
        self.assertEqual(
            resolved["raw_base_url"],
            "https://raw.githubusercontent.com/betty2859/media_tool/main",
        )

    def test_stale_stored_main_yields_catalog_master_branch(self):
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/grandfoxx/my_reading_summary",
            "branch": "main",
            "update_channel": "branch",
            "update_channel_explicit": 0,
        }
        self.manager._catalog_get_update_path_selection_enabled = lambda db_type: False
        self.manager._catalog_default_branch_for_repo = lambda parsed: "master"
        self.manager._fetch_repository_default_branch = lambda parsed, db_type=None: (_ for _ in ()).throw(AssertionError("remote lookup should not run"))

        resolved = self.manager._resolve_update_ref(
            "my_reading_summary",
            "https://raw.githubusercontent.com/grandfoxx/my_reading_summary/master",
            ["my_reading_summary.py", "VERSION"],
            "general",
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["ref_name"], "master")
        self.assertEqual(
            resolved["raw_base_url"],
            "https://raw.githubusercontent.com/grandfoxx/my_reading_summary/master",
        )

    def test_explicit_git_url_branch_wins_over_catalog_default(self):
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/example/demo/tree/dev",
            "branch": "main",
            "update_channel": "branch",
            "update_channel_explicit": 0,
        }
        self.manager._catalog_get_update_path_selection_enabled = lambda db_type: False
        self.manager._catalog_default_branch_for_repo = lambda parsed: "master"
        self.manager._fetch_repository_default_branch = lambda parsed, db_type=None: "master"

        resolved = self.manager._resolve_update_ref(
            "demo",
            "https://raw.githubusercontent.com/example/demo/master",
            ["demo.py", "VERSION"],
            "general",
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["ref_name"], "dev")
        self.assertEqual(
            resolved["raw_base_url"],
            "https://raw.githubusercontent.com/example/demo/dev",
        )

    def test_same_repository_manifest_branch_beats_stale_stored_branch_when_default_unknown(self):
        parsed = self.manager._parse_git_repo("https://github.com/example/demo")
        self.manager._catalog_default_branch_for_repo = lambda parsed: None
        self.manager._fetch_repository_default_branch = lambda parsed, db_type=None: None

        branch = self.manager._resolve_source_branch(
            parsed,
            {"branch": "main"},
            "https://raw.githubusercontent.com/example/demo/master",
            "general",
        )

        self.assertEqual(branch, "master")

    def test_installed_invalid_catalog_source_keeps_validation_state(self):
        installed = {
            "id": "jazzradio",
            "name": "Jazz Radio",
            "version": "1.6.1",
            "git_url": "https://github.com/colaiuta77/jazzradio",
            "has_update_manifest": False,
            "is_installed": True,
        }
        invalid_row = {
            "full_name": "colaiuta77/jazzradio",
            "html_url": "https://github.com/colaiuta77/jazzradio",
            "description": "Jazz Radio",
            "topics": "[]",
            "default_branch": "main",
            "pushed_at": "2026-09-08T06:24:16Z",
            "plugin_id": "jazzradio",
            "plugin_name": "Jazz Radio",
            "latest_version": "1.6.1",
            "is_valid": "invalid",
            "last_checked": "2026-09-13T14:36:27Z",
            "install_error": None,
            "source": "github",
            "base_url": "https://github.com",
        }
        self.manager._catalog_list_valid_repos = lambda db_type=None: []
        self.manager._catalog_list_repos = lambda db_type=None, valid_only=False: [invalid_row]
        self.manager._catalog_get_allow_invalid_install = lambda db_type: True
        self.manager._catalog_meta_dict = lambda db_type: {}

        merged, _ = self.manager._merge_catalog_plugins([installed], "general")

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["catalog_status"], "invalid")
        self.assertFalse(merged[0]["catalog_valid"])
        self.assertIn("Provider 계약", merged[0]["catalog_validation_message"])

    def test_installed_catalog_state_uses_exact_source_not_same_id_other_repo(self):
        installed = {
            "id": "demo",
            "git_url": "https://github.com/example/current",
            "has_update_manifest": False,
            "is_installed": True,
        }
        other_row = {
            "full_name": "other/demo",
            "html_url": "https://github.com/other/demo",
            "plugin_id": "demo",
            "plugin_name": "Demo",
            "latest_version": "1.0.0",
            "is_valid": "invalid",
            "source": "github",
            "base_url": "https://github.com",
        }
        self.manager._catalog_list_valid_repos = lambda db_type=None: []
        self.manager._catalog_list_repos = lambda db_type=None, valid_only=False: [other_row]
        self.manager._catalog_get_allow_invalid_install = lambda db_type: True
        self.manager._catalog_meta_dict = lambda db_type: {}

        merged, _ = self.manager._merge_catalog_plugins([installed], "general")

        self.assertNotIn("catalog_status", merged[0])
        self.assertNotIn("catalog_valid", merged[0])

    def test_installed_card_uses_source_based_update_controls(self):
        script = (Path(__file__).resolve().parents[1] / "script.js").read_text(encoding="utf-8")
        self.assertIn("pm-installed-validation-badge", script)
        self.assertIn("검증 실패</span>", script)
        self.assertIn("p.is_installed && (p.git_url || p.has_update_manifest)", script)
        self.assertIn("catalogMeta.update_path_selection_enabled && p.git_url", script)
        self.assertNotIn("업데이트 정보 없음", script)
        self.assertNotIn("updateManifestBadgeHtml", script)

    def test_replace_modal_closes_before_waiting_for_replace_request(self):
        script = (Path(__file__).resolve().parents[1] / "script.js").read_text(encoding="utf-8")
        start = script.index("modal.querySelector('.pm-replace-confirm').addEventListener")
        end = script.index("    // 버전 비교 헬퍼", start)
        handler = script[start:end]

        self.assertLess(
            handler.index("close();"),
            handler.index("await callPluginAction({ action: 'replace_git'"),
        )
        self.assertIn("저장소 변경을 진행 중입니다", handler)
        self.assertNotIn("this.disabled = false;", handler)

    def test_invalid_uninstalled_catalog_entry_remains_visible_but_blocked(self):
        invalid_row = {
            "full_name": "example/broken",
            "html_url": "https://github.com/example/broken",
            "description": "Broken plugin",
            "topics": "[]",
            "default_branch": "main",
            "pushed_at": "2026-09-13T12:00:00Z",
            "plugin_id": "broken",
            "plugin_name": "Broken",
            "latest_version": "1.2.3",
            "is_valid": "invalid",
            "last_checked": "2026-09-13T14:00:00Z",
            "install_error": None,
            "source": "github",
            "base_url": "https://github.com",
        }
        self.manager._catalog_list_valid_repos = lambda db_type=None: []
        self.manager._catalog_list_repos = lambda db_type=None, valid_only=False: [invalid_row]
        self.manager._catalog_get_allow_invalid_install = lambda db_type: False
        self.manager._catalog_meta_dict = lambda db_type: {}

        merged, _ = self.manager._merge_catalog_plugins([], "general")

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], "broken")
        self.assertFalse(merged[0]["catalog_valid"])
        self.assertFalse(merged[0]["catalog_install_allowed"])
        self.assertIn("Provider 계약", merged[0]["catalog_validation_message"])

    def test_invalid_uninstalled_catalog_entry_can_offer_risk_install_when_enabled(self):
        invalid_row = {
            "full_name": "example/broken",
            "html_url": "https://github.com/example/broken",
            "description": "Broken plugin",
            "topics": "[]",
            "default_branch": "main",
            "pushed_at": "2026-09-13T12:00:00Z",
            "plugin_id": "broken",
            "plugin_name": "Broken",
            "latest_version": None,
            "is_valid": "invalid",
            "last_checked": "2026-09-13T14:00:00Z",
            "install_error": None,
            "source": "github",
            "base_url": "https://github.com",
        }
        self.manager._catalog_list_valid_repos = lambda db_type=None: []
        self.manager._catalog_list_repos = lambda db_type=None, valid_only=False: [invalid_row]
        self.manager._catalog_get_allow_invalid_install = lambda db_type: True
        self.manager._catalog_meta_dict = lambda db_type: {}

        merged, _ = self.manager._merge_catalog_plugins([], "general")

        self.assertEqual(len(merged), 1)
        self.assertFalse(merged[0]["catalog_valid"])
        self.assertTrue(merged[0]["catalog_install_allowed"])


    def test_manifestless_git_source_can_check_update(self):
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/example/demo",
            "branch": "main",
        }
        self.manager._resolve_update_ref = lambda *args, **kwargs: {
            "mode": "branch",
            "ref_name": "main",
            "raw_base_url": "https://raw.githubusercontent.com/example/demo/main",
            "parsed": {"type": "github", "host": "github.com"},
        }
        self.manager._fetch_remote_plugin_version = lambda *args, **kwargs: "2.0.0"

        has_update, latest, status = self.manager._check_plugin_update_detail(
            "demo", "1.0.0", {"update_manifest": None}, "general"
        )

        self.assertTrue(has_update)
        self.assertEqual(latest, "2.0.0")
        self.assertEqual(status, "ok")

    def test_full_folder_update_removes_old_code_without_touching_external_data(self):
        plugins_root = self.root / "plugins"
        dest = plugins_root / "metadata" / "demo"
        target = self.root / "package" / "demo"
        work = plugins_root / "data" / "plugin_manager" / "work"
        data_dir = plugins_root / "data" / "demo"
        cache_dir = plugins_root / "cache" / "demo"
        for d in (dest, target, work, data_dir, cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        write_provider(dest)
        write_provider(target)
        (dest / "old_only.js").write_text("old", encoding="utf-8")
        (target / "new_only.js").write_text("new", encoding="utf-8")
        (target / "VERSION").write_text('{"plugin version": "2.0.0"}\n', encoding="utf-8")
        (data_dir / "state.db").write_text("persistent", encoding="utf-8")
        (cache_dir / "thumb.bin").write_text("cache", encoding="utf-8")

        class Gateway:
            value = "1"
            def get_setting(self, key, default=None): return self.value
            def set_setting(self, key, value): self.value = value

        gateway = Gateway()
        self.manager._get_work_dir = lambda: str(work)
        self.manager._read_git_source_info = lambda plugin_id: {
            "git_url": "https://github.com/example/demo", "branch": "main"
        }
        self.manager._prepare_plugin_data_snapshot = lambda plugin_id: {
            "existed": False, "path": None
        }
        self.manager._cleanup_plugin_data_snapshot = lambda snapshot: None
        self.manager._write_rollback_snapshot = lambda *args, **kwargs: True
        self.manager.get_db_gateway = lambda db_type: gateway
        self.manager._hot_reload_plugin = lambda plugin_id: True

        ok, message = self.manager._update_existing_from_zip(
            str(target), str(dest), "demo", [], "general"
        )

        self.assertTrue(ok, message)
        self.assertFalse((dest / "old_only.js").exists())
        self.assertTrue((dest / "new_only.js").is_file())
        self.assertEqual((data_dir / "state.db").read_text(encoding="utf-8"), "persistent")
        self.assertEqual((cache_dir / "thumb.bin").read_text(encoding="utf-8"), "cache")

    def test_self_update_validation_does_not_require_manifest(self):
        current = self.root / "current_pm"
        target = self.root / "target_pm"
        for root, version in ((current, "1.0.0"), (target, "2.0.0")):
            root.mkdir(parents=True)
            (root / "plugin_manager.py").write_text(
                "from plugins.metadata.base import BaseMetadataProvider\n"
                "class PM(BaseMetadataProvider):\n"
                "    id = 'plugin_manager'\n"
                "    name = 'Plugin Manager'\n"
                "    is_searchable = False\n"
                "    config_schema = []\n"
                "    def search(self, db_type, query): return []\n"
                "    def apply(self, db_type, book_id, item_data): return True, 'ok'\n",
                encoding="utf-8",
            )
            (root / "VERSION").write_text(
                '{"plugin version": "' + version + '"}\n', encoding="utf-8"
            )

        ok, error, version = self.manager._validate_self_update_package(
            str(target), str(current)
        )

        self.assertTrue(ok, error)
        self.assertIsNone(error)
        self.assertEqual(version, "2.0.0")

    def test_filter_ui_uses_installed_only_activation_and_complete_features(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "script.js").read_text(encoding="utf-8")
        index = (root / "index.html").read_text(encoding="utf-8")
        style = (root / "style.css").read_text(encoding="utf-8")

        self.assertIn("const installedPlugins = allPlugins.filter(p => p.is_installed);", script)
        self.assertIn("currentStatusFilter === 'enabled' && !(p.is_installed && p.enabled)", script)
        self.assertIn("currentStatusFilter === 'disabled' && !(p.is_installed && !p.enabled)", script)
        self.assertIn('id="pm-status-filter"', index)
        self.assertIn('id="pm-feature-filter"', index)
        self.assertIn('id="pm-status-enabled" value="enabled"', index)
        self.assertIn('id="pm-status-disabled" value="disabled"', index)

        expected_filters = {
            "category": "카테고리 뷰",
            "widget": "대시보드 위젯",
            "detail-view": "상세 뷰",
            "detail-sidebar": "상세 사이드바",
            "home-widget": "홈",
            "searchable": "수동 검색",
        }
        for filter_id, label in expected_filters.items():
            self.assertIn(f'value="{filter_id}"', index)
            self.assertIn(label, index)
            self.assertIn(f"currentFeatureFilter === '{filter_id}'", script)

        self.assertNotIn('class="pm-filter-groups"', index)
        self.assertIn('.pm-filter-select {', style)
        self.assertIn('.pm-search-box {', style)
        controls_rule = style.split('.pm-controls-bar {', 1)[1].split('}', 1)[0]
        self.assertIn('flex-direction: row;', controls_rule)
        search_rule = style.split('.pm-search-box {', 1)[1].split('}', 1)[0]
        self.assertIn('flex: 1 1 320px;', search_rule)



if __name__ == "__main__":
    unittest.main()
