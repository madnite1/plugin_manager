#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plugin_manager 카탈로그 기능 검증 하네스 (호스트 스텁, 컨테이너 불필요)

설계 문서: .hermes/plans/2026-08-13_catalog-feature.md
스킬: bookoasis-plugin-development (호스트 스텁 검증 패턴)

시나리오:
  A. 설정 정규화/클램프 (간격 1~24, 토픽 형식/중복/빈 목록)
  B. save_config → gateway.set_setting 키/값 검증 + catalog.db에 설정 미저장 (sqlite 분리)
  C. refresh_once — Search API mock → repos upsert/판별/버전 기록
  D. merge — 설치 판정 (폴더 생성/삭제 시 is_installed 전환), invalid 제외, 기존 필드 회귀
  E. rate limit 응답 → refresh_state=error + 다음 주기 재시도 가능
  F. get_dashboard_data 통합 (catalog_meta 포함, count 정확)
  G. DB 동시 접근 (Lock) — 스레드 다수로 read/write 동시 수행
"""
import os
import sys
import json
import shutil
import sqlite3
import tempfile
import threading
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
    stub_root = tempfile.mkdtemp(prefix="hermes-cat-stub-")
    pkg_dir = os.path.join(stub_root, "plugins", "metadata")
    os.makedirs(pkg_dir, exist_ok=True)
    with open(os.path.join(stub_root, "plugins", "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(pkg_dir, "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(pkg_dir, "base.py"), "w") as f:
        f.write(
            "class BaseMetadataProvider:\n"
            "    id = 'base'\n"
            "    name = 'base'\n"
            "    is_searchable = False\n"
            "    config_schema = []\n"
            "    def __init__(self):\n"
            "        self._fake_gateways = {}\n"
            "        self._fake_settings = {}\n"
            "    def get_db_gateway(self, db_type):\n"
            "        return _FakeGateway(self._fake_settings)\n"
            "class _FakeGateway:\n"
            "    def __init__(self, store):\n"
            "        self._store = store\n"
            "    def get_setting(self, key, default=None):\n"
            "        if key not in self._store:\n"
            "            return default\n"
            "        return {'value': self._store[key]}\n"
            "    def set_setting(self, key, value):\n"
            "        self._store[key] = str(value)\n"
        )
    return stub_root

# ── 플러그인 모듈 로드 ──────────────────────────────────────────
stub_root = make_stub_base()
sys.path.insert(0, stub_root)

# services 스텁 — _list_plugins의 MetadataFactory import 대비
fake_services = mock.MagicMock()
fake_metadata_factory = mock.MagicMock()
fake_metadata_factory._discover_provider_classes.return_value = []
fake_metadata_factory._load_plugin_ui_bundle.return_value = None
fake_services.metadata_factory = fake_metadata_factory
sys.modules["services"] = fake_services
sys.modules["services.metadata_factory"] = fake_metadata_factory

spec = {}
spec["_PM_SKIP_AUTO_START"] = True  # 모듈 끝 자동 스레드 시작 차단 (하네스 실행 환경)
with open(SRC, "r", encoding="utf-8") as f:
    exec(compile(f.read(), SRC, "exec"), spec)
PluginManager = spec["PluginManagerMetadataProvider"]

def make_provider(base_dir):
    inst = PluginManager.__new__(PluginManager)
    inst._fake_settings = {}
    inst._get_plugins_base_dir = lambda: base_dir
    inst.get_db_gateway = lambda db_type: _FakeGateway(inst._fake_settings)
    return inst

class _FakeGateway:
    def __init__(self, store):
        self._store = store
    def get_setting(self, key, default=None):
        if key not in self._store:
            return default
        return {"value": self._store[key]}
    def set_setting(self, key, value):
        self._store[key] = str(value)

WORK = tempfile.mkdtemp(prefix="hermes-cat-work-")
BASE_DIR = os.path.join(WORK, "plugins_metadata")
os.makedirs(os.path.join(BASE_DIR, "plugin_manager"), exist_ok=True)
P = make_provider(BASE_DIR)

# =================================================================
section("A. 설정 정규화/클램프")
# -----------------------------------------------------------------
clamp = P._catalog_clamp_interval
check("간격 0 → 1 (최소 클램프)", clamp("0") == 1, f"got {clamp('0')}")
check("간격 1 → 1", clamp("1") == 1)
check("간격 6 → 6", clamp("6") == 6)
check("간격 24 → 24", clamp("24") == 24)
check("간격 25 → 24 (최대 클램프)", clamp("25") == 24, f"got {clamp('25')}")
check("간격 'abc' → 6 (기본)", clamp("abc") == 6)
check("간격 '' → 6 (기본)", clamp("") == 6)
check("간격 None → 6 (기본)", clamp(None) == 6)

norm = P._catalog_normalize_topics
check("토픽 기본 단일", norm("bookoasis-plugin") == ["bookoasis-plugin"])
check("토픽 대문자 → 소문자", norm("BookOasis-Plugin") == ["bookoasis-plugin"])
check("토픽 중복 제거", norm("a,b,a, b") == ["a", "b"], f"got {norm('a,b,a, b')}")
check("토픽 형식 위반 제거", norm("good_topic,Bad!Topic,-bad,ok-topic") == ["ok-topic"],
      f"got {norm('good_topic,Bad!Topic,-bad,ok-topic')}")
check("토픽 빈 목록 → 기본값", norm("  , , ") == ["bookoasis-plugin"])
check("토픽 None → 기본값", norm(None) == ["bookoasis-plugin"])
check("토픽 list 입력", norm(["x", "y"]) == ["x", "y"])
check("토픽 5개 초과 → 5개 제한", len(norm(",".join(f"t{i}" for i in range(8)))) == 5)
check("토픽 줄바꿈 구분", norm("a\nb\nc") == ["a", "b", "c"])

# =================================================================
section("B. save_config — gateway.set_setting + catalog.db 분리")
# -----------------------------------------------------------------
ok, msg = P._catalog_save_config({"refresh_interval_hours": "0", "topics": "bookoasis, BOOKOASIS-PLUGIN, bad!"}, "general")
check("save_config 성공", ok, msg)
check("간격 0 → 1 저장", P._fake_settings.get("PM_CATALOG_REFRESH_HOURS") == "1",
      f"got {P._fake_settings.get('PM_CATALOG_REFRESH_HOURS')}")
check("토픽 정규화 저장", P._fake_settings.get("PM_CATALOG_TOPICS") == "bookoasis,bookoasis-plugin",
      f"got {P._fake_settings.get('PM_CATALOG_TOPICS')}")

ok, msg = P._catalog_save_config({"refresh_interval_hours": "30", "topics": "  "}, "general")
check("간격 30 → 24 클램프", P._fake_settings.get("PM_CATALOG_REFRESH_HOURS") == "24")
check("토픽 빈 값 → 기본값 저장", P._fake_settings.get("PM_CATALOG_TOPICS") == "bookoasis-plugin")

ok, msg = P._catalog_save_config({"refresh_interval_hours": "6", "topics": ",".join(f"t{i}" for i in range(7))}, "general")
saved = P._fake_settings.get("PM_CATALOG_TOPICS")
check("토픽 7개 입력 → 5개만 저장", ok and saved == "t0,t1,t2,t3,t4",
      f"got {saved}")

# catalog.db에 설정 저장 안 됨 (sqlite 분리 검증)
P._catalog_init_db()
db_path = P._get_catalog_db_path()
conn = sqlite3.connect(db_path)
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
conn.close()
check("catalog.db 테이블 = repos, meta만", tables == {"repos", "meta"}, f"got {tables}")
check("catalog.db에 설정 키 없음", "settings" not in tables)

# =================================================================
section("C. refresh_once — Search API mock → repos upsert/판별")
# -----------------------------------------------------------------
def fake_search(topic):
    return {"total_count": 2, "items": [
        {"full_name": "javara999/naverkakaoridi",
         "html_url": "https://github.com/javara999/naverkakaoridi",
         "description": "웹툰/웹소설 검색 플러그인",
         "topics": ["bookoasis", "bookoasis-plugin"],
         "default_branch": "main", "pushed_at": "2026-08-01T00:00:00Z"},
        {"full_name": "colaiuta77/activity_desk",
         "html_url": "https://github.com/colaiuta77/activity_desk",
         "description": "활동 요약 위젯",
         "topics": ["bookoasis-plugin"],
         "default_branch": "main", "pushed_at": "2026-07-20T00:00:00Z"},
    ]}

def fake_check(full_name, default_branch):
    if full_name == "javara999/naverkakaoridi":
        return "valid", "naverkakaoridi", "1.6.3", "네이버 카카오 리디 통합 검색"
    if full_name == "colaiuta77/activity_desk":
        return "valid", "activity_desk", "1.2.0", "활동 데스크"
    return "invalid", None, None, None

with mock.patch.object(P, "_catalog_search_topic", side_effect=fake_search), \
     mock.patch.object(P, "_catalog_check_repo_version", side_effect=fake_check):
    ok, msg = P._catalog_refresh_once("general")

check("refresh_once 성공", ok, msg)
rows = P._catalog_db_query("SELECT * FROM repos ORDER BY full_name")
check("repos 2개 upsert", len(rows) == 2, f"got {len(rows)}")
by_name = {r["full_name"]: r for r in rows}
r1 = by_name.get("javara999/naverkakaoridi")
check("판별 valid + 버전 기록", r1 and r1["is_valid"] == "valid" and r1["latest_version"] == "1.6.3",
      f"got {r1 and (r1['is_valid'], r1['latest_version'])}")
check("plugin_id 기록", r1 and r1["plugin_id"] == "naverkakaoridi")
check("plugin_name 기록 (수집)", r1 and r1["plugin_name"] == "네이버 카카오 리디 통합 검색",
      f"got {r1 and r1.get('plugin_name')}")
check("topics JSON 저장", r1 and json.loads(r1["topics"]) == ["bookoasis", "bookoasis-plugin"])
meta = P._catalog_read_meta()
check("meta last_refresh 기록", bool(meta.get("last_refresh")))
check("meta refresh_state=idle", meta.get("refresh_state") == "idle")

# 중복 실행 방지 (running 중)
P._catalog_set_meta("refresh_state", "running")
with mock.patch.object(P, "_catalog_search_topic", side_effect=AssertionError("should not run")):
    P._catalog_refresh_once("general")  # running → 즉시 return
check("running 중이면 재실행 안 함", P._catalog_read_meta().get("refresh_state") == "running")
P._catalog_set_meta("refresh_state", "idle")

# =================================================================
section("D. merge — 설치 판정 + invalid 제외 + 회귀")
# -----------------------------------------------------------------
P._fake_settings.clear()  # B의 저장값 리셋 — 기본 간격 6 / 기본 토픽으로 복귀
# invalid repo 추가 (bookoasis_mate 패턴)
P._catalog_db_execute(
    "INSERT INTO repos(full_name, html_url, description, topics, default_branch, pushed_at, plugin_id, plugin_name, latest_version, is_valid, last_checked) "
    "VALUES('colaiuta77/bookoasis_mate', 'https://github.com/colaiuta77/bookoasis_mate', '헬퍼', '[]', 'main', '2026-07-01T00:00:00Z', 'bookoasis_mate', NULL, NULL, 'invalid', NULL)"
)
# activity_desk — 수집된 이름 포함 (merge 응답 name 검증용)
P._catalog_db_execute(
    "UPDATE repos SET plugin_name='활동 데스크' WHERE full_name='colaiuta77/activity_desk'"
)
# 설치된 플러그인 시뮬레이션 — plugin_manager 폴더 존재 (list_plugins 스텁 대신 직접 목록 구성)
installed = [
    {"id": "plugin_manager", "name": "플러그인 매니저", "version": "1.0.7", "latest_version": "1.0.7",
     "has_update": False, "enabled": True, "is_searchable": False, "is_category": True, "is_widget": False,
     "has_update_manifest": True, "has_config": True, "is_system": True, "git_url": None, "is_installed": True},
    {"id": "naverkakaoridi", "name": "naverkakaoridi", "version": "1.6.3", "latest_version": "1.6.3",
     "has_update": False, "enabled": True, "is_searchable": True, "is_category": False, "is_widget": False,
     "has_update_manifest": True, "has_config": False, "is_system": False, "git_url": "https://github.com/javara999/naverkakaoridi",
     "is_installed": True},
]
merged, cm = P._merge_catalog_plugins(installed, "general")

ids = [p["id"] for p in merged]
check("설치 항목 유지 (회귀)", "plugin_manager" in ids and "naverkakaoridi" in ids)
check("미설치 카탈로그 병합", "activity_desk" in ids, f"got {ids}")
check("invalid 저장소 제외", "bookoasis_mate" not in ids)
check("count에 invalid 미포함", len(merged) == 3, f"got {len(merged)}")

ad = next((p for p in merged if p["id"] == "activity_desk"), None)
check("미설치 카드 필드: is_installed=false", ad and ad["is_installed"] is False)
check("미설치 카드 필드: git_url", ad and ad["git_url"] == "https://github.com/colaiuta77/activity_desk")
check("미설치 카드 필드: latest_version", ad and ad["latest_version"] == "1.2.0")
check("미설치 카드 필드: name=수집 이름", ad and ad["name"] == "활동 데스크", f"got {ad and ad.get('name')}")
check("미설치 카드 필드: catalog 메타", ad and ad["catalog"] and ad["catalog"]["full_name"] == "colaiuta77/activity_desk")
check("catalog_meta: topics (MariaDB)", cm["topics"] == ["bookoasis-plugin"], f"got {cm['topics']}")
check("catalog_meta: refresh_state", cm["refresh_state"] == "idle")
check("catalog_meta: interval", cm["refresh_interval_hours"] == 6)

# 설치 판정 — 폴더/목록에 없던 repo가 설치되면 목록에서 빠짐 (동적 판정)
installed2 = list(installed) + [
    {"id": "activity_desk", "name": "activity_desk", "version": "1.2.0", "latest_version": "1.2.0",
     "has_update": False, "enabled": True, "is_searchable": False, "is_category": False, "is_widget": True,
     "has_update_manifest": True, "has_config": False, "is_system": False,
     "git_url": "https://github.com/colaiuta77/activity_desk", "is_installed": True},
]
merged2, _ = P._merge_catalog_plugins(installed2, "general")
check("설치된 repo는 미설치 목록에서 제외 (즉시 반영)",
      all(p["id"] != "activity_desk" or p["is_installed"] for p in merged2))

# =================================================================
section("D-2. stale 정리 — 검색에서 사라진 미설치 repo DB 제거")
# -----------------------------------------------------------------
# 설치된 플러그인은 검색 결과와 무관하게 repos에 유지 (폴더 존재 = 설치)
install_dir = os.path.join(BASE_DIR, "activity_desk")
os.makedirs(install_dir, exist_ok=True)

def fake_search_only_one(topic):
    return {"total_count": 1, "items": [
        {"full_name": "javara999/naverkakaoridi",
         "html_url": "https://github.com/javara999/naverkakaoridi",
         "description": "웹툰/웹소설 검색 플러그인",
         "topics": ["bookoasis-plugin"],
         "default_branch": "main", "pushed_at": "2026-08-01T00:00:00Z"},
    ]}

with mock.patch.object(P, "_catalog_search_topic", side_effect=fake_search_only_one), \
     mock.patch.object(P, "_catalog_check_repo_version", side_effect=fake_check):
    ok, msg = P._catalog_refresh_once("general")
names = {r["full_name"] for r in P._catalog_db_query("SELECT full_name FROM repos")}
check("설치된 repo는 검색에 없어도 유지", "colaiuta77/activity_desk" in names, f"got {names}")

# 설치 폴더 제거 후 refresh → 미설치 repo 삭제
shutil.rmtree(install_dir, ignore_errors=True)
with mock.patch.object(P, "_catalog_search_topic", side_effect=fake_search_only_one), \
     mock.patch.object(P, "_catalog_check_repo_version", side_effect=fake_check):
    ok, msg = P._catalog_refresh_once("general")
names = {r["full_name"] for r in P._catalog_db_query("SELECT full_name FROM repos")}
check("검색에서 사라진 미설치 repo 삭제", "colaiuta77/activity_desk" not in names, f"got {names}")
check("여전히 검색되는 repo 유지", "javara999/naverkakaoridi" in names, f"got {names}")
check("정리 메시지에 개수 포함", "미설치 정리" in msg, msg)

# =================================================================
section("E. rate limit → error 상태 + 재시도 가능")
# -----------------------------------------------------------------
with mock.patch.object(P, "_catalog_search_topic",
                       return_value={"message": "API rate limit exceeded for ...", "documentation_url": "..."}):
    try:
        P._catalog_refresh_once("general")
        check("rate limit 시 예외 전파", False, "no exception")
    except RuntimeError:
        check("rate limit 시 예외 전파", True)
meta = P._catalog_read_meta()
check("refresh_state=error 기록", meta.get("refresh_state") == "error", f"got {meta.get('refresh_state')}")
check("refresh_error 기록", bool(meta.get("refresh_error")))
# error 상태는 running이 아니므로 다음 호출 재실행 가능
with mock.patch.object(P, "_catalog_search_topic", side_effect=fake_search), \
     mock.patch.object(P, "_catalog_check_repo_version", side_effect=fake_check):
    ok, msg = P._catalog_refresh_once("general")
check("다음 주기 재시도 성공 (error → idle)", ok and P._catalog_read_meta().get("refresh_state") == "idle")

# =================================================================
section("E-2. 플러그인 메타 수집 (VERSION 버전 전용 + Provider AST)")
# -----------------------------------------------------------------
# 1) VERSION은 공식 `plugin version` 값만 버전 정보로 사용
v = P._parse_remote_version('{"plugin version": "2.0.0"}', "plugin version")
check("VERSION plugin version 파싱", v == "2.0.0", f"got {v}")

# 2) Provider Python 소스 AST에서 공식 id/name 클래스 속성 추출
def fake_fetch_src(url, timeout=15, token=None):
    return (
        "from plugins.metadata.base import BaseMetadataProvider\n"
        "class SamplePlugin(BaseMetadataProvider):\n"
        "    id = 'sample'\n"
        "    name = '샘플 위젯'\n"
        "    def search(self, db_type, query):\n"
        "        return []\n"
    )
with mock.patch.object(P, "_fetch_text", side_effect=fake_fetch_src):
    pid2, nm2 = P._catalog_fetch_plugin_meta("owner/sample", "main", "sample")
check("Provider AST id/name 추출", pid2 == "sample" and nm2 == "샘플 위젯", f"got {(pid2, nm2)}")

# 3) name 없는 Provider → id만 사용
def fake_fetch_none(url, timeout=15, token=None):
    return "class X:\n    id = 'x'\n    pass\n"
with mock.patch.object(P, "_fetch_text", side_effect=fake_fetch_none):
    pid3, nm3 = P._catalog_fetch_plugin_meta("owner/x", "main", "x")
check("Provider name 없음 → id만 추출", pid3 == "x" and nm3 is None, f"got {(pid3, nm3)}")

# 4) check_repo_version 통합 — VERSION에서는 버전, Provider 소스에서는 id/name 수집
with mock.patch.object(P, "_fetch_text", side_effect=[
    '{"plugin version": "1.2.0"}',
    "class B(BaseMetadataProvider):\n    id='plugb'\n    name='플러그 비'\n",
]):
    rv = P._catalog_check_repo_version("owner/plugb", "main")
check("check_repo_version: VERSION/Provider 계약 분리", rv == ("valid", "plugb", "1.2.0", "플러그 비"),
      f"got {rv}")

# 5) 구버전 스키마(plugin_name 없음) → ALTER 자동 추가
_old_db = os.path.join(tempfile.mkdtemp(prefix="hermes-verify-"), "catalog.db")
_conn = sqlite3.connect(_old_db)
_conn.execute(
    "CREATE TABLE repos (full_name TEXT PRIMARY KEY, html_url TEXT, description TEXT, "
    "topics TEXT, default_branch TEXT, pushed_at TEXT, plugin_id TEXT, latest_version TEXT, "
    "is_valid TEXT DEFAULT 'unknown', last_checked TEXT)"
)
_conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
_conn.commit()
_conn.close()
with mock.patch.object(P, "_get_catalog_db_path", return_value=_old_db):
    P._catalog_init_db()
_conn2 = sqlite3.connect(_old_db)
_new_cols = {r[1] for r in _conn2.execute("PRAGMA table_info(repos)")}
_conn2.close()
shutil.rmtree(os.path.dirname(_old_db), ignore_errors=True)
check("구버전 DB plugin_name/install_error 자동 추가", "plugin_name" in _new_cols and "install_error" in _new_cols,
      f"cols={sorted(_new_cols)}")

# =================================================================
section("E-3. install_error — 설치 실패 기록/클리어/갱신 리셋/응답 포함")
# -----------------------------------------------------------------
# 1) 실패 메시지 저장 (GitHub URL → full_name 파싱)
P._catalog_record_install_error(
    "https://github.com/javara999/naverkakaoridi", "검증 실패:\n- 금지 패턴: eval() 호출 발견"
)
row = P._catalog_db_query(
    "SELECT install_error FROM repos WHERE full_name='javara999/naverkakaoridi'"
)[0]
check("실패 메시지 저장", row["install_error"] == "검증 실패:\n- 금지 패턴: eval() 호출 발견",
      f"got {row['install_error']!r}")

# 2) 비GitHub URL → 저장 안 함
P._catalog_record_install_error("https://gitea.example.com/u/r", "저장되면 안 됨")
rows = P._catalog_db_query("SELECT COUNT(*) AS c FROM repos WHERE install_error IS NOT NULL")
check("비GitHub URL은 저장 안 함", rows[0]["c"] == 1, f"got {rows[0]['c']}")

# 3) 2000자 절단
P._catalog_record_install_error("https://github.com/javara999/naverkakaoridi", "x" * 5000)
row = P._catalog_db_query(
    "SELECT install_error FROM repos WHERE full_name='javara999/naverkakaoridi'"
)[0]
check("2000자 절단", len(row["install_error"]) == 2000, f"got {len(row['install_error'])}")

# 4) 갱신(upsert) 시 install_error 리셋
with mock.patch.object(P, "_catalog_search_topic", side_effect=fake_search), \
     mock.patch.object(P, "_catalog_check_repo_version", side_effect=fake_check):
    P._catalog_refresh_once("general")
row = P._catalog_db_query(
    "SELECT install_error FROM repos WHERE full_name='javara999/naverkakaoridi'"
)[0]
check("갱신 시 install_error 리셋", row["install_error"] is None,
      f"got {row['install_error']!r}")

# 5) 실패 재기록 + 목록 SELECT 응답 포함
P._catalog_record_install_error("https://github.com/javara999/naverkakaoridi", "네트워크 오류")
r_valid = P._catalog_list_valid_repos()
r_nav = next((x for x in r_valid if x["full_name"] == "javara999/naverkakaoridi"), None)
check("목록 SELECT에 install_error 포함", r_nav and r_nav["install_error"] == "네트워크 오류",
      f"got {r_nav and r_nav.get('install_error')}")

# 6) merge 응답 install_error 포함 + 성공/클리어
P._catalog_db_execute(
    "UPDATE repos SET install_error='클래스 id 불일치' WHERE full_name='colaiuta77/activity_desk'"
)
merged3, _ = P._merge_catalog_plugins(installed, "general")
ad3 = next((p for p in merged3 if p["id"] == "activity_desk"), None)
check("merge 응답 install_error 포함", ad3 and ad3["install_error"] == "클래스 id 불일치",
      f"got {ad3 and ad3.get('install_error')}")
check("merge 응답 오류 없으면 None", all(p.get("install_error") is None
      for p in merged3 if p["id"] != "activity_desk"))
P._catalog_clear_install_error("https://github.com/colaiuta77/activity_desk")
row = P._catalog_db_query(
    "SELECT install_error FROM repos WHERE full_name='colaiuta77/activity_desk'"
)[0]
check("성공 시 install_error 클리어", row["install_error"] is None,
      f"got {row['install_error']!r}")
# F 섹션이 activity_desk 레코드를 사용하므로 레코드는 유지 (install_error만 리셋 상태) 

# =================================================================
section("F. get_dashboard_data 통합")
# -----------------------------------------------------------------
with mock.patch.object(P, "_ensure_catalog_thread"), \
     mock.patch.object(P, "_ensure_catalog_routes"), \
     mock.patch.object(P, "_list_plugins", return_value=installed):
    resp = P.get_dashboard_data("general")
check("get_dashboard_data success", resp.get("success") is True)
check("catalog_meta 포함", isinstance(resp.get("catalog_meta"), dict) and resp["catalog_meta"]["refresh_state"] == "idle")
check("plugins 통합", len(resp.get("plugins", [])) == 3)
ad_resp = next((p for p in resp.get("plugins", []) if p["id"] == "activity_desk"), None)
check("통합 응답 name=수집 이름", ad_resp and ad_resp["name"] == "활동 데스크",
      f"got {ad_resp and ad_resp.get('name')}")
check("count 정확 (invalid 제외)", resp.get("count") == 3, f"got {resp.get('count')}")

# =================================================================
section("G. DB 동시 접근 (Lock)")
# -----------------------------------------------------------------
errors = []
def writer(n):
    try:
        for i in range(30):
            P._catalog_db_execute(
                "INSERT OR REPLACE INTO repos(full_name, html_url, description, topics, default_branch, pushed_at, plugin_id, latest_version, is_valid, last_checked) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"t{n}/repo{i}", "", "", "[]", "main", "", f"repo{i}", "1.0.0", "valid", ""))
    except Exception as e:
        errors.append(f"writer{n}: {e}")

def reader(n):
    try:
        for _ in range(30):
            P._catalog_db_query("SELECT COUNT(*) AS c FROM repos")
            P._catalog_read_meta()
    except Exception as e:
        errors.append(f"reader{n}: {e}")

threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)] + \
          [threading.Thread(target=reader, args=(i,)) for i in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("동시 read/write 예외 없음", not errors, f"errors: {errors[:2]}")

# =================================================================
# 정리
# =================================================================
sys.path.remove(stub_root)
shutil.rmtree(stub_root, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

print(f"\n결과: {PASS} 통과 / {FAIL} 실패")
sys.exit(1 if FAIL else 0)
