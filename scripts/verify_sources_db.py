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

spec = {"__file__": SRC}
spec["_PM_SKIP_AUTO_START"] = True  # 모듈 끝 자동 스레드 시작 차단 (하네스 실행 환경)
stub_root = make_stub_base()
sys.path.insert(0, stub_root)
with open(SRC, "r", encoding="utf-8") as f:
    exec(compile(f.read(), SRC, "exec"), spec)
PluginManager = spec["PluginManagerMetadataProvider"]

def make_provider(base_dir):
    inst = PluginManager.__new__(PluginManager)
    inst._get_plugins_base_dir = lambda: base_dir
    inst._get_data_dir = lambda: os.path.join(base_dir, "plugin_manager")
    fake_gateway = mock.MagicMock()
    fake_gateway.set_setting.return_value = None
    fake_gateway.get_setting.return_value = None
    inst.get_db_gateway = lambda _db_type: fake_gateway
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
section("F2. 동일 ID ZIP 업데이트 — 런타임 데이터 보존 + 관리 파일 정리")
installed = os.path.join(BASE_DIR, "testplugin")
# 기존 설치본에 런타임 데이터와 구버전 관리 파일을 만든다.
with open(os.path.join(installed, "runtime.db"), "w", encoding="utf-8") as f:
    f.write("사용자 데이터")
with open(os.path.join(installed, "legacy.py"), "w", encoding="utf-8") as f:
    f.write("LEGACY = True\n")
with open(os.path.join(installed, "testplugin.py"), "r", encoding="utf-8") as f:
    old_src = f.read()
old_src = old_src.replace(
    "'files': ['testplugin.py', '__init__.py', 'VERSION']",
    "'files': ['testplugin.py', '__init__.py', 'VERSION', 'legacy.py']",
)
with open(os.path.join(installed, "testplugin.py"), "w", encoding="utf-8") as f:
    f.write(old_src)

# 신규 ZIP은 legacy.py를 제거하고 new_module.py를 관리 파일로 추가한다.
upd_src = os.path.join(WORK, "zip_update_src")
write_test_plugin(upd_src, with_manifest=True)
with open(os.path.join(upd_src, "testplugin.py"), "r", encoding="utf-8") as f:
    upd_code = f.read()
upd_code = upd_code.replace(
    "'files': ['testplugin.py', '__init__.py', 'VERSION']",
    "'files': ['testplugin.py', '__init__.py', 'VERSION', 'new_module.py']",
)
with open(os.path.join(upd_src, "testplugin.py"), "w", encoding="utf-8") as f:
    f.write(upd_code)
with open(os.path.join(upd_src, "new_module.py"), "w", encoding="utf-8") as f:
    f.write("NEW_MODULE = True\n")

ok, msg = P._install_from_zip(make_zip_bytes(upd_src), "testplugin.zip", "general")
check("동일 ID ZIP을 업데이트로 처리", ok and "안전하게 업데이트" in str(msg), msg)
check("런타임 데이터 보존", os.path.isfile(os.path.join(installed, "runtime.db"))
      and open(os.path.join(installed, "runtime.db"), encoding="utf-8").read() == "사용자 데이터")
check("신규 관리 파일 추가", os.path.isfile(os.path.join(installed, "new_module.py")))
check("제거된 관리 파일 삭제", not os.path.exists(os.path.join(installed, "legacy.py")))
check("ZIP 업데이트 백업 정리", not os.path.exists(os.path.join(BASE_DIR, ".pm_zip_backup_testplugin")))

# 로드 검증 실패를 강제로 만들어 전체 롤백을 확인한다.
with open(os.path.join(installed, "VERSION"), "r", encoding="utf-8") as f:
    before_version = f.read()
with open(os.path.join(installed, "runtime.db"), "r", encoding="utf-8") as f:
    before_runtime = f.read()
fake_metadata_factory.get_available_providers.return_value = []
try:
    ok, msg = P._install_from_zip(make_zip_bytes(upd_src), "testplugin.zip", "general")
finally:
    fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
check("로드 실패 시 업데이트 실패", not ok and "자동 복원" in str(msg), msg)
check("로드 실패 시 VERSION 복원", open(os.path.join(installed, "VERSION"), encoding="utf-8").read() == before_version)
check("로드 실패 시 런타임 데이터 복원", open(os.path.join(installed, "runtime.db"), encoding="utf-8").read() == before_runtime)
check("롤백 후 백업 정리", not os.path.exists(os.path.join(BASE_DIR, ".pm_zip_backup_testplugin")))

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
check("catalog.db 테이블 = repos, meta, settings", cat_tables == {"repos", "meta", "settings"}, f"got {cat_tables}")
check("catalog.db에 plugin_sources 없음", "plugin_sources" not in cat_tables)

# =================================================================
section("J. DB 파일 git 무시")
try:
    r = subprocess.run(
        ["git", "-c", f"safe.directory={PLUGIN_DIR}", "check-ignore", "--no-index", "-q", "plugin_sources.db"],
        cwd=PLUGIN_DIR, capture_output=True, text=True, timeout=15,
    )
    check(".gitignore *.db 매칭 (plugin_sources.db)", r.returncode == 0,
          f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
    r2 = subprocess.run(
        ["git", "-c", f"safe.directory={PLUGIN_DIR}", "ls-files", "--error-unmatch", "plugin_sources.db"],
        cwd=PLUGIN_DIR, capture_output=True, text=True, timeout=15,
    )
    check("plugin_sources.db 미추적", r2.returncode != 0, "tracked!")
except FileNotFoundError:
    check(".gitignore *.db 매칭 (plugin_sources.db)", False, "git 없음")

# =================================================================
section("K. _parse_raw_base_url — refs/heads 전체 브랜치 표기")
# .../owner/repo/refs/heads/<branch>[/subpath] 형태는 branch를 <branch>로 파싱해야
# 서브디렉토리(monorepo)로 오판되어 백필이 누락되지 않는다.
_parse_cases = [
    ("https://raw.githubusercontent.com/yume-script/plugin_board/refs/heads/main",
     ("yume-script", "plugin_board", "main", "")),
    ("https://raw.githubusercontent.com/yume-script/unified_book/refs/heads/main/",
     ("yume-script", "unified_book", "main", "")),
    ("https://raw.githubusercontent.com/yume-script/unified_book/refs/heads/dev/sub/dir",
     ("yume-script", "unified_book", "dev", "sub/dir")),
    # 기존 표기 회귀
    ("https://raw.githubusercontent.com/madnite1/plugin_manager/main",
     ("madnite1", "plugin_manager", "main", "")),
    ("https://raw.githubusercontent.com/grandfoxx/my_reading_summary/master",
     ("grandfoxx", "my_reading_summary", "master", "")),
    ("https://raw.githubusercontent.com/leeyj/BookOasis_stable/main/plugins/metadata/stats_dashboard",
     ("leeyj", "BookOasis_stable", "main", "plugins/metadata/stats_dashboard")),
]
for _url, _exp in _parse_cases:
    _got = P._parse_raw_base_url(_url)
    check(f"parse {_url}", _got == _exp, f"got {_got}")

# refs/heads 표기 raw_base_url → 소스 메타 백필 통합 검증
_refs_info = P._ensure_git_source_from_raw_base_url(
    "refsplug",
    "https://raw.githubusercontent.com/yume-script/plugin_board/refs/heads/main",
    ["plugin_board.py", "__init__.py"],
)
check("refs/heads 백필 git_url", _refs_info and _refs_info.get("git_url")
      == "https://github.com/yume-script/plugin_board", f"got {_refs_info}")
check("refs/heads 백필 branch", _refs_info and _refs_info.get("branch") == "main",
      f"got {_refs_info}")
check("refs/heads 백필 DB 저장", P._sources_get("refsplug") is not None)

# =================================================================
section("L. update_manifest.files 정합성 — 배포 파일 누락 금지")
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


# =================================================================
section("M. 온라인 업데이트 — ZIP 우선 + 최신 manifest + 제한적 raw fallback")
online_id = "onlineplug"
online_dir = os.path.join(BASE_DIR, online_id)
os.makedirs(online_dir, exist_ok=True)
old_manifest = {
    "enabled": True,
    "provider": "github-raw",
    "raw_base_url": "https://raw.githubusercontent.com/owner/onlineplug/main",
    "files": ["onlineplug.py", "__init__.py", "VERSION", "legacy.py"],
    "version_file": "VERSION",
    "version_key": "plugin version",
}
with open(os.path.join(online_dir, "onlineplug.py"), "w", encoding="utf-8") as f:
    f.write(
        "from plugins.metadata.base import BaseMetadataProvider\n\n"
        "class OnlinePlug(BaseMetadataProvider):\n"
        "    id = 'onlineplug'\n"
        "    name = '온라인 테스트'\n"
        "    is_searchable = False\n"
        f"    update_manifest = {old_manifest!r}\n"
    )
with open(os.path.join(online_dir, "__init__.py"), "w", encoding="utf-8") as f:
    f.write("from .onlineplug import OnlinePlug\n")
with open(os.path.join(online_dir, "VERSION"), "w", encoding="utf-8") as f:
    f.write('{"plugin version": "1.0.0"}\n')
with open(os.path.join(online_dir, "legacy.py"), "w", encoding="utf-8") as f:
    f.write("LEGACY = True\n")
with open(os.path.join(online_dir, "runtime.db"), "w", encoding="utf-8") as f:
    f.write("런타임 보존 데이터")
P._sources_set(online_id, {
    "git_url": "https://github.com/owner/onlineplug",
    "branch": "main",
    "installed_at": "2026-01-01T00:00:00",
    "manifest_files": old_manifest["files"],
})

new_src = os.path.join(WORK, "online_update_src")
os.makedirs(new_src, exist_ok=True)
new_manifest = dict(old_manifest)
new_manifest["files"] = ["onlineplug.py", "__init__.py", "VERSION", "new_module.py"]
with open(os.path.join(new_src, "onlineplug.py"), "w", encoding="utf-8") as f:
    f.write(
        "from plugins.metadata.base import BaseMetadataProvider\n\n"
        "class OnlinePlug(BaseMetadataProvider):\n"
        "    id = 'onlineplug'\n"
        "    name = '온라인 테스트'\n"
        "    is_searchable = False\n"
        "    config_schema = []\n"
        f"    update_manifest = {new_manifest!r}\n"
        "    def search(self, db_type, query):\n"
        "        return []\n"
        "    def apply(self, db_type, book_id, item_data):\n"
        "        return False, 'noop'\n"
    )
with open(os.path.join(new_src, "__init__.py"), "w", encoding="utf-8") as f:
    f.write("from .onlineplug import OnlinePlug\n")
with open(os.path.join(new_src, "VERSION"), "w", encoding="utf-8") as f:
    f.write('{"plugin version": "1.1.0"}\n')
with open(os.path.join(new_src, "new_module.py"), "w", encoding="utf-8") as f:
    f.write("NEW_MODULE = True\n")

def _repo_zip_bytes(root, top="repo-main"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(root):
            for fname in sorted(files):
                full = os.path.join(base, fname)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                zf.write(full, f"{top}/{rel}")
    return b.getvalue()

class _OnlineCls:
    update_manifest = old_manifest

saved_download_zip = P._download_repository_zip
saved_raw_update = P._update_plugin_raw_legacy
saved_import_result = fake_metadata_factory._import_provider_module_and_class.return_value
saved_providers = fake_metadata_factory.get_available_providers.return_value
try:
    fake_metadata_factory._import_provider_module_and_class.return_value = (None, _OnlineCls)
    fake_metadata_factory.get_available_providers.return_value = [{"id": online_id}]
    good_zip = _repo_zip_bytes(new_src)
    P._download_repository_zip = lambda _url, _db: ({
        "zip_bytes": good_zip,
        "used_url": "https://codeload.github.com/owner/onlineplug/zip/refs/tags/v1.1.0",
        "ref_type": "release",
        "ref_name": "v1.1.0",
        "branch": "v1.1.0",
        "explicit_branch": False,
    }, None)
    ok, msg = P._update_plugin(online_id, "general")
    check("온라인 업데이트 ZIP 우선 성공", ok and "v1.1.0" in str(msg), msg)
    check("온라인 업데이트 최신 VERSION 적용",
          P._read_local_plugin_version(online_dir, "VERSION", "plugin version") == "1.1.0")
    check("온라인 업데이트 신규 관리 파일 추가", os.path.isfile(os.path.join(online_dir, "new_module.py")))
    check("온라인 업데이트 제거 관리 파일 삭제", not os.path.exists(os.path.join(online_dir, "legacy.py")))
    check("온라인 업데이트 런타임 데이터 보존",
          open(os.path.join(online_dir, "runtime.db"), encoding="utf-8").read() == "런타임 보존 데이터")
    src_info = P._sources_get(online_id)
    check("소스 저장소 유지 + 최신 manifest_files 갱신",
          src_info and src_info.get("git_url") == "https://github.com/owner/onlineplug"
          and "new_module.py" in (src_info.get("manifest_files") or [])
          and "legacy.py" not in (src_info.get("manifest_files") or []), f"got {src_info}")

    # ZIP은 받았지만 plugin_id가 잘못된 경우 raw fallback을 절대 호출하지 않는다.
    bad_src = os.path.join(WORK, "online_bad_id")
    shutil.copytree(new_src, bad_src)
    with open(os.path.join(bad_src, "onlineplug.py"), "r", encoding="utf-8") as f:
        bad_code = f.read().replace("id = 'onlineplug'", "id = 'wrongplug'")
    with open(os.path.join(bad_src, "onlineplug.py"), "w", encoding="utf-8") as f:
        f.write(bad_code)
    bad_zip = _repo_zip_bytes(bad_src)
    P._download_repository_zip = lambda _url, _db: ({
        "zip_bytes": bad_zip, "used_url": "bad", "ref_type": "release",
        "ref_name": "v1.2.0", "branch": "v1.2.0", "explicit_branch": False,
    }, None)
    raw_calls = {"count": 0}
    def _raw_probe(_pid, _db):
        raw_calls["count"] += 1
        return True, "raw 호출"
    P._update_plugin_raw_legacy = _raw_probe
    ok, msg = P._update_plugin(online_id, "general")
    check("ZIP 패키지 검증 실패 시 업데이트 중단", not ok and "플러그인 ID" in str(msg), msg)
    check("ZIP 패키지 검증 실패 시 raw fallback 금지", raw_calls["count"] == 0, f"calls={raw_calls['count']}")

    # ZIP 자체를 받지 못한 경우에만 raw 호환 경로를 허용한다.
    P._download_repository_zip = lambda _url, _db: (None, "저장소 ZIP 다운로드 실패: 404")
    ok, msg = P._update_plugin(online_id, "general")
    check("ZIP 다운로드 자체 실패 시 raw fallback 허용", ok and raw_calls["count"] == 1, msg)
finally:
    P._download_repository_zip = saved_download_zip
    P._update_plugin_raw_legacy = saved_raw_update
    fake_metadata_factory._import_provider_module_and_class.return_value = saved_import_result
    fake_metadata_factory.get_available_providers.return_value = saved_providers



# =================================================================
section("N. Plugin Manager 자기 업데이트 — ZIP/Git/버전/rollback 정책")
self_base = os.path.join(WORK, "self_update_base")
os.makedirs(self_base, exist_ok=True)
PS = make_provider(self_base)
# 실제 운영처럼 코드(metadata/plugin_manager)와 영속 데이터(data/plugin_manager)를 분리한다.
PS._get_data_dir = lambda: os.path.join(self_base, "_data", "plugin_manager")
self_dir = os.path.join(self_base, "plugin_manager")
os.makedirs(self_dir, exist_ok=True)


def write_self_plugin(root, version):
    os.makedirs(root, exist_ok=True)
    code = """from plugins.metadata.base import BaseMetadataProvider
class PluginManagerMetadataProvider(BaseMetadataProvider):
    id = 'plugin_manager'
    name = 'Plugin Manager'
    config_schema = [{'key': 'x'}]
    update_manifest = {
        'enabled': True,
        'provider': 'github-raw',
        'raw_base_url': 'https://raw.githubusercontent.com/madnite1/plugin_manager/main',
        'files': ['plugin_manager.py', '__init__.py', 'VERSION'],
        'version_file': 'VERSION',
        'version_key': 'plugin version',
    }
    def search(self, db_type, query): return []
    def apply(self, db_type, book_id, item_data): return True, 'ok'
"""
    with open(os.path.join(root, "plugin_manager.py"), "w", encoding="utf-8") as f:
        f.write(code)
    with open(os.path.join(root, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(root, "VERSION"), "w", encoding="utf-8") as f:
        json.dump({"plugin version": version}, f)


write_self_plugin(self_dir, "1.14.6")
self_up = os.path.join(WORK, "self_update_147")
write_self_plugin(self_up, "1.14.7")
self_same = os.path.join(WORK, "self_update_same")
write_self_plugin(self_same, "1.14.6")
self_down = os.path.join(WORK, "self_update_down")
write_self_plugin(self_down, "1.14.5")

ok, msg, ver = PS._validate_self_update_package(self_up, self_dir)
check("자기 업데이트 상위 버전 허용", ok and ver == "1.14.7", msg)
ok, msg, _ = PS._validate_self_update_package(self_same, self_dir)
check("자기 업데이트 동일 버전 차단", not ok and "상위 버전만" in str(msg), msg)
ok, msg, _ = PS._validate_self_update_package(self_down, self_dir)
check("자기 업데이트 다운그레이드 차단", not ok and "상위 버전만" in str(msg), msg)

# 실제 트랜잭션 적용 + 로드/버전 확인
class _SelfReloaded:
    id = "plugin_manager"

fake_metadata_factory.get_available_providers.return_value = [{"id": "plugin_manager"}]
fake_metadata_factory._import_provider_module_and_class.return_value = (None, _SelfReloaded)
try:
    ok, msg = PS._update_existing_from_zip(
        self_up, self_dir, "plugin_manager", [], "general", force=False
    )
finally:
    fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
check("자기 업데이트 ZIP 트랜잭션 성공", ok, msg)
check("자기 업데이트 VERSION 적용", json.load(open(os.path.join(self_dir, "VERSION"), encoding="utf-8"))["plugin version"] == "1.14.7")
check("자기 업데이트 백업 정리", not os.path.exists(os.path.join(self_base, ".pm_zip_backup_plugin_manager")))

# 로드 검증 실패 시 rollback
self_up2 = os.path.join(WORK, "self_update_148")
write_self_plugin(self_up2, "1.14.8")
before_self_version = open(os.path.join(self_dir, "VERSION"), encoding="utf-8").read()
fake_metadata_factory.get_available_providers.return_value = []
try:
    ok, msg = PS._update_existing_from_zip(
        self_up2, self_dir, "plugin_manager", [], "general", force=False
    )
finally:
    fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
check("자기 업데이트 로드 실패 시 rollback", not ok and "자동 복원" in str(msg), msg)
check("자기 업데이트 rollback VERSION 복원", open(os.path.join(self_dir, "VERSION"), encoding="utf-8").read() == before_self_version)

# 온라인 자기 업데이트는 ZIP 획득 실패 시 raw fallback 금지
class _SelfCurrentCls:
    id = "plugin_manager"
    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/madnite1/plugin_manager/main",
        "files": ["plugin_manager.py", "__init__.py", "VERSION"],
        "version_file": "VERSION",
        "version_key": "plugin version",
    }

PS._sources_set("plugin_manager", {
    "git_url": "https://github.com/madnite1/plugin_manager",
    "branch": "main",
    "installed_at": "2026-01-01T00:00:00",
    "manifest_files": ["plugin_manager.py", "__init__.py", "VERSION"],
})
raw_called = {"v": False}
def _self_raw_probe(_pid, _db):
    raw_called["v"] = True
    return True, "raw"

fake_metadata_factory._import_provider_module_and_class.return_value = (None, _SelfCurrentCls)
with mock.patch.object(PS, "_download_repository_zip", return_value=(None, "archive 실패")), \
     mock.patch.object(PS, "_update_plugin_raw_legacy", side_effect=_self_raw_probe):
    ok, msg = PS._update_plugin("plugin_manager", "general")
check("자기 온라인 업데이트 ZIP 실패 시 중단", not ok and "raw fallback 없이" in str(msg), msg)
check("자기 온라인 업데이트 raw fallback 금지", not raw_called["v"])

# Git URL로 기존 Plugin Manager 업데이트 시 폴더 삭제 대신 트랜잭션 경로 사용
write_self_plugin(self_dir, "1.14.7")
self_git_up = os.path.join(WORK, "self_git_148")
write_self_plugin(self_git_up, "1.14.8")
self_git_zip = _repo_zip_bytes(self_git_up, "plugin_manager-main")
fake_metadata_factory.get_available_providers.return_value = [{"id": "plugin_manager"}]
fake_metadata_factory._import_provider_module_and_class.return_value = (None, _SelfReloaded)
try:
    with mock.patch.object(PS, "_download_repository_zip", return_value=({
            "zip_bytes": self_git_zip,
            "used_url": "https://codeload.github.com/madnite1/plugin_manager/zip/refs/heads/main",
            "ref_type": "branch",
            "ref_name": "main",
            "branch": "main",
        }, None)), \
         mock.patch.object(PS, "_validate_plugin_source", return_value=(True, [])):
        ok, msg = PS._install_from_git("https://github.com/madnite1/plugin_manager", "general")
finally:
    fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
check("기존 Plugin Manager Git URL 업데이트 성공", ok and "안전하게 업데이트" in str(msg), msg)
check("Git URL 자기 업데이트 VERSION 적용", json.load(open(os.path.join(self_dir, "VERSION"), encoding="utf-8"))["plugin version"] == "1.14.8")
self_src_info = PS._sources_get("plugin_manager")
check("Git URL 자기 업데이트 소스 메타 갱신", self_src_info and self_src_info.get("git_url") == "https://github.com/madnite1/plugin_manager", self_src_info)

# 성공 업데이트 직전 버전 롤백 슬롯 + 양방향 복원 검증
rollback_info = PS._read_rollback_info("plugin_manager")
check("성공 업데이트 후 롤백 슬롯 생성", rollback_info is not None)
check("롤백 슬롯 직전 버전 기록", rollback_info and rollback_info.get("from_version") == "1.14.7", rollback_info)
# 기본 OFF에서는 롤백 액션을 차단하고, 설정 ON 후에만 허용한다.
check("롤백 기능 기본 OFF", not PS._catalog_get_rollback_enabled("general"))
off_ok, off_msg = PS._rollback_plugin("plugin_manager", "general")
check("롤백 기능 OFF 액션 차단", not off_ok and "비활성화" in str(off_msg), off_msg)
saved_rollback_enabled = PS._catalog_get_rollback_enabled
PS._catalog_get_rollback_enabled = lambda _db: True
check("롤백 기능 설정 ON", PS._catalog_get_rollback_enabled("general"))
with open(os.path.join(self_dir, "runtime.keep"), "w", encoding="utf-8") as f:
    f.write("현재 런타임 데이터")
fake_metadata_factory.get_available_providers.return_value = [{"id": "plugin_manager"}]
try:
    ok, msg = PS._rollback_plugin("plugin_manager", "general")
finally:
    fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
check("사용자 롤백 성공", ok and "1.14.7" in str(msg), msg)
check("사용자 롤백 VERSION 복원", json.load(open(os.path.join(self_dir, "VERSION"), encoding="utf-8"))["plugin version"] == "1.14.7")
check("롤백 시 관리 밖 런타임 파일 보존", open(os.path.join(self_dir, "runtime.keep"), encoding="utf-8").read() == "현재 런타임 데이터")
rollback_info2 = PS._read_rollback_info("plugin_manager")
check("롤백 성공 후 현재 버전을 재복구 슬롯으로 교체", rollback_info2 and rollback_info2.get("from_version") == "1.14.8", rollback_info2)
fake_metadata_factory.get_available_providers.return_value = [{"id": "plugin_manager"}]
try:
    ok2, msg2 = PS._rollback_plugin("plugin_manager", "general")
finally:
    fake_metadata_factory.get_available_providers.return_value = [{"id": "testplugin"}]
check("롤백 후 다시 최신 버전 복구", ok2 and json.load(open(os.path.join(self_dir, "VERSION"), encoding="utf-8"))["plugin version"] == "1.14.8", msg2)
PS._catalog_get_rollback_enabled = saved_rollback_enabled


# =================================================================
section("O. VERSION 공식 규약 — plugin version + x.y.z")
version_base = os.path.join(WORK, "version_contract")
os.makedirs(version_base, exist_ok=True)
PV = make_provider(version_base)


def write_version_contract_plugin(root, version_payload):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "sample.py"), "w", encoding="utf-8") as f:
        f.write("""from plugins.metadata.base import BaseMetadataProvider
class SampleProvider(BaseMetadataProvider):
    id = 'sample'
    name = '샘플'
    is_searchable = False
    config_schema = []
    update_manifest = {
        'enabled': True,
        'provider': 'github-raw',
        'raw_base_url': 'https://raw.githubusercontent.com/example/sample/main',
        'files': ['sample.py', '__init__.py', 'VERSION'],
        'version_file': 'VERSION',
        'version_key': 'plugin version',
    }
    def search(self, db_type, query): return []
    def apply(self, db_type, book_id, item_data): return True, 'ok'
""")
    with open(os.path.join(root, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(root, "VERSION"), "w", encoding="utf-8") as f:
        if isinstance(version_payload, str):
            f.write(version_payload)
        else:
            json.dump(version_payload, f, ensure_ascii=False)


def version_check(payload):
    root = os.path.join(version_base, "case")
    if os.path.isdir(root):
        shutil.rmtree(root)
    write_version_contract_plugin(root, payload)
    ok, checks = PV._validate_plugin_source(root, "sample")
    vc = next((c for c in checks if c.get("name") == "VERSION"), {})
    return ok, vc

ok, vc = version_check({"plugin version": "1.2.3"})
check("VERSION 공식 plugin version 허용", ok and vc.get("ok") is True, vc)
ok, vc = version_check({"version": "1.2.3"})
check("VERSION 비공식 version 키 거부", not ok and vc.get("ok") is False and "plugin version" in str(vc.get("detail")), vc)
ok, vc = version_check({"plugin version": "abc"})
check("VERSION 비표준 버전 문자열 거부", not ok and vc.get("ok") is False and "x.y.z" in str(vc.get("detail")), vc)
ok, vc = version_check(["1.2.3"])
check("VERSION JSON 객체 아닌 형식 거부", not ok and vc.get("ok") is False and "JSON 객체" in str(vc.get("detail")), vc)
ok, vc = version_check({"plugin version": "v1.2.3-beta.1"})
check("VERSION v 접두사/프리릴리즈 허용", ok and vc.get("ok") is True, vc)

print()
if FAIL:
    print(f"결과: {FAIL}개 실패 / {PASS}개 통과")
    sys.exit(1)
print(f"결과: {PASS}개 전부 통과 (소스 메타 DB 검증)")
sys.exit(0)
