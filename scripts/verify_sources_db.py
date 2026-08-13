#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_manager 소스 메타 DB 검증 하네스 (호스트 스텁, 컨테이너 불필요)

배경: .git_source/.zip_source 파일 대신 sqlite plugin_sources.db에 소스 메타 저장.
  - 설치(zip/git) 시 파일 미생성 + DB 저장
  - 설치 후 최초 1회 레거시 파일 → DB 마이그레이션 (meta legacy_migration_done)
  - DB는 git 저장소에 미포함 (.gitignore *.db)

시나리오:
  A. DB 스키마/CRUD (plugin_sources + meta 테이블, upsert/read/delete)
  B. 마이그레이션 1회 — .git_source/.zip_source 파일 → DB 이동 + 파일 삭제 + done 마커
  C. 마이그레이션 재실행 무동작 (done 후 새 파일은 그대로)
  D. _read_git_source_info — DB에서 읽기 (파일 불필요)
  E. _ensure_git_source_from_raw_base_url — 파일 미생성 + DB 저장 (monorepo subpath 제외)
  F. zip 설치 — .zip_source 파일 미생성 + DB 레코드 저장
  G. git 설치 — .git_source 파일 미생성 + DB 레코드 저장
  H. _delete_plugin — DB 레코드 정리
  I. catalog.db와 분리 (catalog.db에 plugin_sources 테이블 없음)
  J. DB 파일 git 무시 (.gitignore *.db 매칭)
"""
import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from unittest import mock

# ── 실행 환경 보호 ──────────────────────────────────────────────
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)          # plugin_manager/
SRC = os.path.join(PLUGIN_DIR, "plugin_manager.py")

PASS = 0
FAIL = 0

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")

def section(name):
    print(f"\n== {name} ==")

# ── 스텁: plugins.metadata.base ─────────────────────────────────
def make_stub_base():
    stub_root = tempfile.mkdtemp(prefix="hermes-src-stub-")
    pkg_dir = os.path.join(stub_root, "plugins", "metadata")
    os.makedirs(pkg_dir, exist_ok=True)
    for p in (os.path.join(stub_root, "plugins", "__init__.py"),
              os.path.join(pkg_dir, "__init__.py")):
        with open(p, "w") as f:
            f.write("")
    with open(os.path.join(pkg_dir, "base.py"), "w") as f:
        f.write(
            "class BaseMetadataProvider:\n"
            "    id = 'base'\n"
            "    name = 'base'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
        )
    return stub_root

# ── services 스텁 (설치 시 PluginService/MetadataFactory import 대비) ──
fake_services = mock.MagicMock()
fake_plugin_service = mock.MagicMock()
fake_plugin_service.toggle_plugin_enabled.return_value = (True, "ok")
fake_metadata_factory = mock.MagicMock()
fake_metadata_factory.hot_reload_plugin.return_value = None
fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
fake_metadata_factory._discover_provider_classes.return_value = []
fake_metadata_factory._load_plugin_ui_bundle.return_value = None
# `from services.metadata_factory import MetadataFactory` 가 같은 mock을 보도록 자가 참조
fake_metadata_factory.MetadataFactory = fake_metadata_factory
fake_plugin_service.PluginService = fake_plugin_service
fake_services.plugin_service = fake_plugin_service
fake_services.metadata_factory = fake_metadata_factory
sys.modules["services"] = fake_services
sys.modules["services.plugin_service"] = fake_plugin_service
sys.modules["services.metadata_factory"] = fake_metadata_factory

spec = {}
stub_root = make_stub_base()
sys.path.insert(0, stub_root)
with open(SRC, "r", encoding="utf-8") as f:
    exec(compile(f.read(), SRC, "exec"), spec)
PluginManager = spec["PluginManagerMetadataProvider"]

def make_provider(base_dir):
    inst = PluginManager.__new__(PluginManager)
    inst._get_plugins_base_dir = lambda: base_dir
    return inst

# ── 검증 통과 미니 플러그인 소스 ─────────────────────────────────
TEST_MANIFEST_BODY = (
    "    update_manifest = {\n"
    "        'enabled': True,\n"
    "        'provider': 'github-raw',\n"
    "        'raw_base_url': 'https://raw.githubusercontent.com/owner/testplugin/main',\n"
    "        'files': ['testplugin.py', '__init__.py', 'VERSION'],\n"
    "        'version_file': 'VERSION',\n"
    "        'version_key': 'plugin version',\n"
    "    }\n"
)
TEST_VERSION = '{\n  "plugin version": "1.0.0"\n}\n'
TEST_INIT = "from .testplugin import TestPlugin\n"

def write_test_plugin(root, with_manifest=False):
    os.makedirs(root, exist_ok=True)
    manifest_attr = TEST_MANIFEST_BODY if with_manifest else ""
    with open(os.path.join(root, "testplugin.py"), "w", encoding="utf-8") as f:
        f.write(
            "from plugins.metadata.base import BaseMetadataProvider\n"
            "\n"
            "class TestPlugin(BaseMetadataProvider):\n"
            "    id = 'testplugin'\n"
            "    name = '테스트 플러그인'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
            + manifest_attr
            + "\n"
            "    def search(self, db_type, query):\n"
            "        return []\n"
            "\n"
            "    def apply(self, db_type, book_id, item_data):\n"
            "        return False, 'noop'\n"
        )
    with open(os.path.join(root, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(TEST_INIT)
    with open(os.path.join(root, "VERSION"), "w", encoding="utf-8") as f:
        f.write(TEST_VERSION)

def make_zip_bytes(root):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(root)):
            zf.write(os.path.join(root, fname), fname)
    return base64.b64encode(buf.getvalue()).decode("ascii")

# =================================================================
WORK = tempfile.mkdtemp(prefix="hermes-src-work-")
BASE_DIR = os.path.join(WORK, "plugins_metadata")
os.makedirs(os.path.join(BASE_DIR, "plugin_manager"), exist_ok=True)
P = make_provider(BASE_DIR)
PM_DIR = os.path.join(BASE_DIR, "plugin_manager")
SOURCES_DB = os.path.join(PM_DIR, "plugin_sources.db")
CATALOG_DB = os.path.join(PM_DIR, "catalog.db")

def db_tables(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()

# =================================================================
section("A. DB 스키마/CRUD")
P._sources_init_db()
check("plugin_sources.db 생성", os.path.isfile(SOURCES_DB))
tables = db_tables(SOURCES_DB)
check("테이블 = plugin_sources, meta", tables == {"plugin_sources", "meta"}, f"got {tables}")

P._sources_set("plug_a", {"git_url": "https://github.com/o/a",
                         "branch": "main", "installed_at": "2026-01-01T00:00:00",
                         "manifest_files": ["a.py"]})
info = P._sources_get("plug_a")
check("git_url 저장/조회", info and info.get("git_url") == "https://github.com/o/a"
      and info.get("branch") == "main", f"got {info}")
check("manifest_files 복원", info and info.get("manifest_files") == ["a.py"], f"got {info}")

P._sources_set("plug_b", {"source_type": "zip_upload", "filename": "p.zip",
                          "installed_at": "2026-01-01T00:00:00"})
check("git_url 없는 입력 → 저장 안 함", P._sources_get("plug_b") is None)
check("없는 id → None", P._sources_get("nope") is None)

P._sources_set("plug_a", {"git_url": "https://github.com/o/a2"})
check("upsert 갱신", P._sources_get("plug_a").get("git_url") == "https://github.com/o/a2")
P._sources_delete("plug_a")
check("delete 후 제거", P._sources_get("plug_a") is None)

# =================================================================
section("B. 마이그레이션 1회 — 레거시 파일 → DB")
# 레거시 파일 재현 (파일 기반 시절 설치본)
legacy_git = {"git_url": "https://github.com/owner/old",
              "branch": "v1.0.0", "installed_at": "2026-01-01T00:00:00",
              "manifest_files": ["old.py"]}
legacy_zip = {"source_type": "zip_upload", "filename": "manual.zip",
              "installed_at": "2026-01-02T00:00:00"}
os.makedirs(os.path.join(BASE_DIR, "oldplug"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "zipplug"), exist_ok=True)
with open(os.path.join(BASE_DIR, "oldplug", ".git_source"), "w", encoding="utf-8") as f:
    json.dump(legacy_git, f)
with open(os.path.join(BASE_DIR, "zipplug", ".zip_source"), "w", encoding="utf-8") as f:
    json.dump(legacy_zip, f)

P._sources_migrate_legacy_files()
check("git 레거시 → DB", P._sources_get("oldplug") == legacy_git, f"got {P._sources_get('oldplug')}")
check("zip 레거시 → 레코드 없음 (git_url 없음, 백필 재판단 대상)",
      P._sources_get("zipplug") is None, f"got {P._sources_get('zipplug')}")
check(".git_source 파일 삭제", not os.path.exists(os.path.join(BASE_DIR, "oldplug", ".git_source")))
check(".zip_source 파일 삭제", not os.path.exists(os.path.join(BASE_DIR, "zipplug", ".zip_source")))
check("meta done 기록", P._sources_meta_get("legacy_migration_done") == "1")

# =================================================================
section("B2. update_manifest 기반 백필 (수동 설치본 — _source 파일 없음)")
# ① manifest 있음 (GitHub 루트) → git_url 백필 기대
man_src = os.path.join(WORK, "man_manifest")
write_test_plugin(man_src, with_manifest=True)
shutil.copytree(man_src, os.path.join(BASE_DIR, "manual_manifest"), dirs_exist_ok=True)
# ② manifest 없음 → 레코드 없음 기대
man_none = os.path.join(WORK, "man_none")
write_test_plugin(man_none, with_manifest=False)
shutil.copytree(man_none, os.path.join(BASE_DIR, "manual_nomanifest"), dirs_exist_ok=True)
# ③ monorepo 서브디렉토리 raw_base_url → 레코드 없음 기대 (릴리즈 태그 기준 불일치)
man_sub = os.path.join(WORK, "man_sub")
write_test_plugin(man_sub, with_manifest=True)
with open(os.path.join(man_sub, "testplugin.py"), "r", encoding="utf-8") as f:
    src = f.read()
src = src.replace(
    "https://raw.githubusercontent.com/owner/testplugin/main",
    "https://raw.githubusercontent.com/o/repo/main/plugins/metadata/sub",
)
with open(os.path.join(man_sub, "testplugin.py"), "w", encoding="utf-8") as f:
    f.write(src)
shutil.copytree(man_sub, os.path.join(BASE_DIR, "manual_subpath"), dirs_exist_ok=True)

# done 리셋 후 재마이그레이션 (레거시 파일은 이미 처리됨 → 백필만 실행)
P._sources_meta_set("legacy_migration_done", None)
spec["_SOURCES_MIGRATION_DONE"] = False
P._sources_migrate_legacy_files()

m_info = P._sources_get("manual_manifest")
check("manifest 있음 → git_url 백필", m_info and m_info.get("git_url")
      and m_info.get("git_url") == "https://github.com/owner/testplugin"
      and m_info.get("branch") == "main", f"got {m_info}")
check("manifest 없음 → 레코드 없음", P._sources_get("manual_nomanifest") is None)
check("monorepo subpath → 레코드 없음", P._sources_get("manual_subpath") is None)
check("기존 레거시 레코드 보존", P._sources_get("oldplug") == legacy_git)
check("백필 후 done 재기록", P._sources_meta_get("legacy_migration_done") == "1")

# =================================================================
section("C. 마이그레이션 재실행 무동작")
with open(os.path.join(BASE_DIR, "oldplug", ".git_source"), "w", encoding="utf-8") as f:
    json.dump({"source_type": "git_url", "git_url": "https://github.com/owner/late"}, f)
P._sources_migrate_legacy_files()
check("done 후 파일 재스캔 안 함 (파일 유지)", os.path.isfile(os.path.join(BASE_DIR, "oldplug", ".git_source")))
check("DB 레코드 유지 (미변경)", P._sources_get("oldplug") == legacy_git,
      f"got {P._sources_get('oldplug')}")

# =================================================================
section("D. _read_git_source_info — DB 읽기")
check("DB에서 git_url 메타 반환", P._read_git_source_info("oldplug") == legacy_git)
check("레코드 없으면 None", P._read_git_source_info("not_installed") is None)

# =================================================================
section("E. raw_base_url 추론 → DB 저장, 파일 미생성")
info = P._ensure_git_source_from_raw_base_url(
    "newplug", "https://raw.githubusercontent.com/owner/newplug/main", ["n.py"])
check("추론 성공", info and info.get("git_url") == "https://github.com/owner/newplug")
check(".git_source 파일 미생성", not os.path.exists(os.path.join(BASE_DIR, "newplug", ".git_source")))
check("DB 저장 확인", P._sources_get("newplug") == info)
check("monorepo subpath → None", P._ensure_git_source_from_raw_base_url(
    "subplug", "https://raw.githubusercontent.com/o/repo/main/plugins/metadata/x", []) is None)
check("subpath DB 미저장", P._sources_get("subplug") is None)

# =================================================================
section("F. zip 설치 — update_manifest 기준 소스 판단")
# ① manifest 없는 zip → 레코드 없음 (로컬 플러그인 유지)
src_root = os.path.join(WORK, "zip_src")
write_test_plugin(src_root, with_manifest=False)
zip_b64 = make_zip_bytes(src_root)
ok, msg = P._install_from_zip(zip_b64, "testplugin.zip", "general")
check("zip 설치 성공", ok, msg)
check("플러그인 폴더 생성", os.path.isdir(os.path.join(BASE_DIR, "testplugin")))
check(".zip_source 파일 미생성", not os.path.exists(os.path.join(BASE_DIR, "testplugin", ".zip_source")))
check("manifest 없음 → 레코드 없음", P._sources_get("testplugin") is None,
      f"got {P._sources_get('testplugin')}")

# ② manifest 있는 zip → git_url 저장 (설치 방식 무관 — raw_base_url 검증으로 판단)
src_man = os.path.join(WORK, "zip_src_man")
write_test_plugin(src_man, with_manifest=True)
P._install_from_zip(make_zip_bytes(src_man), "testplugin.zip", "general")
zip_info = P._sources_get("testplugin")
check("manifest 있음 → git_url 저장", zip_info and zip_info.get("git_url")
      == "https://github.com/owner/testplugin" and zip_info.get("branch") == "main",
      f"got {zip_info}")

# =================================================================
section("G. git 설치 — .git_source 파일 미생성 + DB 저장")
git_src = os.path.join(WORK, "git_src")
write_test_plugin(git_src, with_manifest=True)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    for fname in sorted(os.listdir(git_src)):
        zf.write(os.path.join(git_src, fname), fname)
zip_bytes = buf.getvalue()

class FakeResp:
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return zip_bytes
    def geturl(self):
        return "https://github.com/owner/testplugin/releases/latest"

def fake_urlopen(req, timeout=None):
    return FakeResp()

# plugin_manager 모듈 전역 urlopen을 인메모리 ZIP 응답으로 교체 (실제 네트워크 없음)
saved_urlopen = spec["urlopen"]
spec["urlopen"] = fake_urlopen
try:
    with mock.patch.object(PluginManager, "_fetch_latest_release_tag", return_value=None), \
         mock.patch.object(PluginManager, "_build_repo_zip_url",
                           return_value=("https://codeload.github.com/owner/testplugin/zip/refs/heads/main", "main")), \
         mock.patch.object(PluginManager, "_zip_url_candidates",
                           return_value=["https://codeload.github.com/owner/testplugin/zip/refs/heads/main"]):
        ok, msg = P._install_from_git("https://github.com/owner/testplugin", "general")
finally:
    spec["urlopen"] = saved_urlopen

check("git 설치 성공", ok, msg)
check(".git_source 파일 미생성", not os.path.exists(os.path.join(BASE_DIR, "testplugin", ".git_source")))
g_info = P._sources_get("testplugin")
check("DB에 git_url 저장", g_info and g_info.get("git_url") == "https://github.com/owner/testplugin"
      and g_info.get("branch") == "main", f"got {g_info}")

# =================================================================
section("H. _delete_plugin — DB 레코드 정리")
ok, msg = P._delete_plugin("testplugin", "general")
check("삭제 성공", ok, msg)
check("DB 레코드 제거", P._sources_get("testplugin") is None)
# 삭제 대상이 아닌 다른 플러그인의 마이그레이션 레코드는 유지 (폴더 존재)
check("삭제 안 한 플러그인 레코드 유지", P._sources_get("oldplug") == legacy_git)

# =================================================================
section("I. catalog.db와 분리")
P._catalog_init_db()
cat_tables = db_tables(CATALOG_DB)
check("catalog.db 테이블 = repos, meta", cat_tables == {"repos", "meta"}, f"got {cat_tables}")
check("catalog.db에 plugin_sources 없음", "plugin_sources" not in cat_tables)

# =================================================================
section("J. DB 파일 git 무시")
try:
    r = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "plugin_sources.db"],
        cwd=PLUGIN_DIR, capture_output=True, text=True, timeout=15,
    )
    check(".gitignore *.db 매칭 (plugin_sources.db)", r.returncode == 0,
          f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
    r2 = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "plugin_sources.db"],
        cwd=PLUGIN_DIR, capture_output=True, text=True, timeout=15,
    )
    check("plugin_sources.db 미추적", r2.returncode != 0, "tracked!")
except FileNotFoundError:
    check(".gitignore *.db 매칭 (plugin_sources.db)", False, "git 없음")

# =================================================================
section("K. update_manifest.files 정합성 — 배포 파일 누락 금지")
# manifest.files에 빠진 배포 파일이 있으면 설치/업데이트 시 _prune_plugin_dir이
# 해당 파일을 삭제하므로, 실제 존재 파일과 목록이 반드시 일치해야 한다.
try:
    import ast as _ast
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(REPO_ROOT, "plugin_manager.py"), encoding="utf-8") as f:
        tree = _ast.parse(f.read())
    manifest_files = []
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Assign)
                and any(isinstance(t, _ast.Name) and t.id == "update_manifest" for t in node.targets)
                and isinstance(node.value, _ast.Dict)):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, _ast.Constant) and k.value == "files" and isinstance(v, _ast.List):
                    manifest_files = [str(e.value) for e in v.elts if isinstance(e, _ast.Constant)]
    check("update_manifest.files 추출", bool(manifest_files), "AST 추출 실패")

    # ① 목록의 모든 파일이 repo 루트에 존재
    missing = [fn for fn in manifest_files if not os.path.isfile(os.path.join(REPO_ROOT, fn))]
    check("목록 내 파일 전부 존재", not missing, f"없는 파일: {missing}")

    # ② repo 루트의 배포 파일(코드/UI/VERSION)이 목록에 전부 포함
    deploy_files = [fn for fn in os.listdir(REPO_ROOT)
                    if fn.endswith((".py", ".html", ".js", ".css")) or fn == "VERSION"]
    not_listed = [fn for fn in deploy_files if fn not in manifest_files]
    check("배포 파일 전부 목록 포함", not not_listed, f"누락: {not_listed}")
except Exception as e:
    check("update_manifest.files 정합성", False, str(e))

print()
if FAIL:
    print(f"결과: {FAIL}개 실패 / {PASS}개 통과")
    sys.exit(1)
print(f"결과: {PASS}개 전부 통과 (소스 메타 DB 검증)")
sys.exit(0)
