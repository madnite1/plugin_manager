# -*- coding: utf-8 -*-
import os
import sys
import ast
import shutil
import json
import re
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import logging
from plugins.metadata.base import BaseMetadataProvider

logger = logging.getLogger(__name__)

# GitHub 릴리즈 태그 조회 TTL 캐시 (releases/latest 리다이렉트는 요청마다 수행하면 느리므로 5분 캐시)
_RELEASE_TAG_CACHE = {}
_RELEASE_TAG_CACHE_TTL = 300  # 초

# ── 플러그인 카탈로그 (GitHub 토픽 기반 자동 수집) ──────────────────────────
# 백그라운드 갱신 스레드 보장 (gunicorn 1워커 전제 — is_alive()로 사망 감지 후 재시작)
_CATALOG_THREAD_LOCK = threading.Lock()
_CATALOG_THREAD = None          # 실행 중인 백그라운드 스레드 참조
_CATALOG_THREAD_ALIVE = False   # 스레드 시작 시도 플래그 (사망 시 루프가 리셋)
# 자체 save-config 라우트 1회 등록 보장 (멀티 워커/재호출 안전)
_CATALOG_ROUTES_LOCK = threading.Lock()
_CATALOG_ROUTES_REGISTERED = False
# catalog.db(sqlite) 동시 접근 직렬화 (갱신 스레드 + 요청 처리)
_CATALOG_DB_LOCK = threading.Lock()

# plugin_sources.db(sqlite) — .git_source/.zip_source 파일 대체 메타 저장소
_SOURCES_DB_LOCK = threading.Lock()
# 레거시 .git_source/.zip_source 파일 → DB 1회 마이그레이션 보장 (gunicorn 1워커 전제)
_SOURCES_MIGRATION_LOCK = threading.Lock()
_SOURCES_MIGRATION_DONE = False

_CATALOG_DEFAULT_TOPICS = ["bookoasis-plugin"]
_CATALOG_DEFAULT_INTERVAL_HOURS = 6
_CATALOG_MIN_INTERVAL_HOURS = 1
_CATALOG_MAX_INTERVAL_HOURS = 24
# 백그라운드 루프 sleep 틱 — 60초 단위로 last_refresh 경과를 재확인해
# 서버 재시작(스레드 재기동)에도 타이머 리셋 없이 interval 도달을 정확히 감지
_CATALOG_LOOP_TICK_SECONDS = 60
_CATALOG_MAX_TOPICS = 5  # GitHub 비인증 Search API 분당 10회 제한 (토픽 수 + VERSION 검증 합계 한도 보호)
_CATALOG_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# VERSION 재검증 TTL: 저장소가 20개를 넘어가면 24시간 이내 검증 결과 재사용
_CATALOG_VERIFY_MAX_REPOS = 20
_CATALOG_VERIFY_TTL_SECONDS = 24 * 3600
# 갱신 실패 후 재시도 쿨다운 — rate limit(403) 등으로 실패 시 60초마다 재시도하면
# 오히려 제한이 풀리지 않아 악순환됨. 실패하면 최소 이 시간(초) 뒤에야 재시도.
_CATALOG_RETRY_COOLDOWN_SECONDS = 10 * 60


class PluginManagerMetadataProvider(BaseMetadataProvider):
    """
    BookOasis 플러그인 매니저
    Git 저장소 URL을 통한 플러그인 동적 설치, 업데이트, 삭제, 활성화 관리 플러그인
    """

    id = "plugin_manager"
    name = "플러그인 매니저"
    is_searchable = False
    config_schema = []

    category_tab = {
        "title": "플러그인 매니저",
        "icon": "fa-solid fa-boxes-stacked",
        "order": 99,
    }

    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": "https://raw.githubusercontent.com/madnite1/plugin_manager/main",
        "files": ["plugin_manager.py", "__init__.py", "VERSION", "index.html", "style.css", "script.js", "settings.html", "settings.js"],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": False,
    }

    def search(self, db_type, query):
        """수동 메타데이터 검색 미지원"""
        return []

    def apply(self, db_type, book_id, item_data):
        """
        플러그인 액션 처리 엔드포인트
        item_data = {
            'action': 'install_zip' | 'install_git' | 'update' | 'update_all' | 'delete' | 'toggle',
            ...
        }
        """
        if not isinstance(item_data, dict):
            return False, "유효하지 않은 요청 데이터 형식입니다."

        action = str(item_data.get("action", "")).strip().lower()

        if action == "install_zip":
            zip_data = str(item_data.get("zip_data", "")).strip()
            filename = str(item_data.get("filename", "")).strip()
            force = item_data.get("force") in (True, 1, "1", "true", "True")
            if not zip_data:
                return False, "ZIP 압축 파일 데이터가 누락되었습니다."
            return self._install_from_zip(zip_data, filename, db_type, force=force)

        elif action == "install_git":
            git_url = str(item_data.get("git_url", "")).strip()
            force = item_data.get("force") in (True, 1, "1", "true", "True")
            if not git_url:
                return False, "Git 저장소 URL이 누락되었습니다."
            ok, msg = self._install_from_git(git_url, db_type, force=force)
            # 카탈로그 저장소라면 설치 결과를 install_error에 기록 (프론트 카드 상태 반영)
            if ok:
                self._catalog_clear_install_error(git_url)
            else:
                self._catalog_record_install_error(git_url, msg)
            return ok, msg

        elif action == "update":
            plugin_id = str(item_data.get("plugin_id", "")).strip()
            if not plugin_id:
                return False, "업데이트할 플러그인 ID가 누락되었습니다."
            return self._update_plugin(plugin_id, db_type)

        elif action == "update_all":
            return self._update_all_plugins(db_type)

        elif action == "delete":
            plugin_id = str(item_data.get("plugin_id", "")).strip()
            if not plugin_id:
                return False, "삭제할 플러그인 ID가 누락되었습니다."
            return self._delete_plugin(plugin_id, db_type)

        elif action == "toggle":
            plugin_id = str(item_data.get("plugin_id", "")).strip()
            enabled = str(item_data.get("enabled", "1")).strip()
            if not plugin_id:
                return False, "플러그인 ID가 누락되었습니다."
            return self._toggle_plugin(plugin_id, enabled, db_type)

        elif action == "check_update":
            plugin_id = str(item_data.get("plugin_id", "")).strip()
            if not plugin_id:
                return False, "업데이트 확인할 플러그인 ID가 누락되었습니다."
            return self._check_update_action(plugin_id, db_type)

        elif action == "catalog_refresh":
            return self._catalog_manual_refresh(db_type)

        elif action == "save_config":
            return self._catalog_save_config(item_data, db_type)

        return False, f"지원하지 않는 액션입니다: {action}"

    def get_dashboard_data(self, db_type, limit=10):
        """
        플러그인 목록 조회 API
        설치된 플러그인 + 카탈로그(미설치, GitHub 토픽 검색) 통합 목록 반환
        """
        try:
            self._ensure_catalog_routes()
            self._ensure_catalog_thread(db_type)
            plugins = self._list_plugins(db_type)
            plugins, catalog_meta = self._merge_catalog_plugins(plugins, db_type)
            return {
                "success": True,
                "plugins": plugins,
                "count": len(plugins),
                "catalog_meta": catalog_meta,
            }
        except Exception as e:
            return {"success": False, "error": f"플러그인 목록 조회 실패: {str(e)}"}

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _get_plugins_base_dir(self):
        """plugins/metadata 루트 경로 반환"""
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _list_plugins(self, db_type):
            """설치된 전체 메타데이터 플러그인 상세 정보 수집"""
            base_dir = self._get_plugins_base_dir()
            plugins = []

            if not os.path.exists(base_dir):
                return plugins

            from services.metadata_factory import MetadataFactory
            discovered_classes = {}
            try:
                for p_name, target_cls in MetadataFactory._discover_provider_classes():
                    p_id = getattr(target_cls, 'id', p_name)
                    discovered_classes[p_id] = target_cls
            except Exception as e:
                print(f"[PluginManager] Discover provider classes error: {e}")

            gateway = self.get_db_gateway(db_type)

            for entry in sorted(os.listdir(base_dir)):
                full_path = os.path.join(base_dir, entry)
                if entry in ('__pycache__', 'base.py') or entry.startswith('.'):
                    continue
                if not os.path.isdir(full_path):
                    continue

                plugin_id = entry
                cls_obj = discovered_classes.get(plugin_id)

                # 1. 버전 읽기
                version = "1.0.0"
                version_file = os.path.join(full_path, "VERSION")
                if os.path.isfile(version_file):
                    try:
                        with open(version_file, "r", encoding="utf-8") as f:
                            vdata = json.load(f)
                            version = vdata.get("plugin version") or vdata.get("version") or "1.0.0"
                    except Exception:
                        pass

                # 2. 메타 정보
                name = getattr(cls_obj, "name", plugin_id) if cls_obj else plugin_id
                is_searchable = getattr(cls_obj, "is_searchable", True) if cls_obj else False
                category_tab = cls_obj.__dict__.get("category_tab", None) if cls_obj else None
                dashboard_widget = getattr(cls_obj, "dashboard_widget", None) if cls_obj else None
                update_manifest = getattr(cls_obj, "update_manifest", None) if cls_obj else None

                # 2-1. 설정 항목 보유 여부 (config_schema 또는 커스텀 설정 UI 번들)
                has_config = False
                if cls_obj is not None:
                    # 클래스 __dict__ 직접 접근: 동적 디스크립터(config_schema descriptor)의
                    # __get__ 실행(DB 조회 등)을 유발하지 않고 선언 유무만 판별
                    raw_schema = getattr(cls_obj, "__dict__", {}).get("config_schema", [])
                    has_config = bool(raw_schema)
                    if not has_config:
                        try:
                            from services.metadata_factory import MetadataFactory
                            has_config = bool(MetadataFactory._load_plugin_ui_bundle(plugin_id, target="settings"))
                        except Exception:
                            has_config = False

                # 3. 활성화 상태
                enabled_raw = gateway.get_setting(f"PLUGIN_ENABLED_{plugin_id}", default="1")
                if isinstance(enabled_raw, dict):
                    enabled_val = str(enabled_raw.get("value", "1"))
                else:
                    enabled_val = str(enabled_raw or "1")
                is_enabled = (enabled_val == "1")

                # 4. 업데이트 체크는 목록 응답에서 제외 (프론트가 check_update 액션으로 비동기 조회)
                has_update, latest_version = False, version

                # 4-1. Git 소스 메타 (설치 경로 표시용 — git_url 유무가 git 소스 판단 기준)
                git_url = None
                git_info = self._read_git_source_info(plugin_id)
                if git_info:
                    git_url = str(git_info.get("git_url") or "").strip() or None

                plugins.append({
                    "id": plugin_id,
                    "name": name,
                    "version": version,
                    "latest_version": latest_version,
                    "has_update": has_update,
                    "enabled": is_enabled,
                    "is_searchable": is_searchable,
                    "is_category": bool(category_tab),
                    "is_widget": bool(dashboard_widget),
                    "has_update_manifest": bool(update_manifest),
                    "has_config": has_config,
                    "is_system": (plugin_id in ("plugin_manager",)),
                    "git_url": git_url,
                    "is_installed": True,
                })

            return plugins

    # ------------------------------------------------------------------
    # Self Update Engine (릴리즈 태그 우선, 브랜치 폴백 — 코어 PluginService 미사용)
    # ------------------------------------------------------------------

    _VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

    def _build_update_spec(self, plugin_id, manifest):
        """update_manifest 검증 + 업데이트 spec 구성 (코어 _validate_update_manifest 축소판)"""
        if not (manifest and isinstance(manifest, dict)):
            return None
        if not manifest.get("enabled"):
            return None
        if str(manifest.get("provider", "") or "").strip() != "github-raw":
            return None
        raw_base_url = str(manifest.get("raw_base_url", "") or "").strip().rstrip("/")
        if not raw_base_url:
            return None
        files_raw = manifest.get("files")
        if not isinstance(files_raw, list) or not files_raw:
            return None
        safe_files = []
        for path in files_raw:
            clean = os.path.normpath(str(path))
            if (clean.startswith("..") or clean.startswith("/") or clean.startswith("\\")
                    or clean in (".", "")):
                return None
            safe_files.append(str(path))
        version_file = str(manifest.get("version_file", "VERSION") or "VERSION").strip()
        version_key = str(manifest.get("version_key", "plugin version") or "plugin version").strip()
        if version_file not in safe_files:
            safe_files.append(version_file)
        return {
            "raw_base_url": raw_base_url,
            "files": safe_files,
            "version_file": version_file,
            "version_key": version_key,
        }

    def _read_git_source_info(self, plugin_id):
        """플러그인 설치 시 저장된 소스 메타(sqlite plugin_sources) 읽기 (없으면 None).

        최초 호출 시 레거시 .git_source/.zip_source 파일 → DB 1회 마이그레이션을 수행한다."""
        try:
            self._sources_migrate_legacy_files()
        except Exception:
            pass
        return self._sources_get(plugin_id)

    def _parse_github_repo(self, git_url):
        """GitHub 저장소 URL에서 (owner, repo) 추출. GitHub가 아니면 None."""
        url = str(git_url or "").strip().rstrip("/")
        url = re.sub(r"\.git$", "", url)
        m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:/tree/([^/]+))?$", url)
        if m:
            return m.group(1), m.group(2)
        return None

    # ---- 저장소 호스트 일반화 (GitHub + Gitea) ----

    def _parse_git_repo(self, git_url):
        """Git 저장소 웹 URL 파싱 → dict (type/owner/repo/branch/subpath) 또는 None.

        - GitHub: https://github.com/owner/repo[/tree/branch]
        - Gitea : https://host/owner/repo[/src/branch/branch]
        반환: {"type": "github"|"gitea", "host": ..., "base": "https://github.com"|"https://host",
               "owner": ..., "repo": ..., "branch": ..., "subpath": ...}
        """
        url = str(git_url or "").strip().rstrip("/")
        url = re.sub(r"\.git$", "", url)
        if not url:
            return None
        m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:/tree/([^/]+))?(?:/.*)?$", url)
        if m:
            return {
                "type": "github",
                "host": "github.com",
                "base": "https://github.com",
                "owner": m.group(1),
                "repo": m.group(2),
                "branch": m.group(3) or None,
                "subpath": "",
            }
        m = re.match(r"^https?://([^/]+)/([^/]+)/([^/]+?)(?:/src/branch/([^/]+))?(?:/.*)?$", url)
        if m:
            return {
                "type": "gitea",
                "host": m.group(1),
                "base": "https://{0}".format(m.group(1)),
                "owner": m.group(2),
                "repo": m.group(3),
                "branch": m.group(4) or None,
                "subpath": "",
            }
        return None

    def _host_zip_base(self, parsed):
        """파싱된 저장소 → (zip_base, tag_base, raw_base, api_base) 튜플.
        - GitHub: codeload / github / raw.githubusercontent / api.github.com
        - Gitea : {base}/archive / {base} / {base}/raw / {base}/api/v1
        """
        if not parsed:
            return None
        if parsed["type"] == "github":
            o, r = parsed["owner"], parsed["repo"]
            return (
                "https://codeload.github.com/{0}/{1}".format(o, r),
                "https://github.com/{0}/{1}".format(o, r),
                "https://raw.githubusercontent.com/{0}/{1}".format(o, r),
                "https://api.github.com",
            )
        base = parsed["base"]
        o, r = parsed["owner"], parsed["repo"]
        return (
            "{0}/{1}/{2}/archive".format(base, o, r),
            "{0}/{1}/{2}".format(base, o, r),
            "{0}/{1}/{2}/raw".format(base, o, r),
            "{0}/api/v1".format(base),
        )

    def _host_branch(self, parsed):
        """브랜치 결정: 명시 → default_branch 설정 → main"""
        return parsed.get("branch") or parsed.get("default_branch") or "main"

    def _fetch_latest_release_tag(self, owner, repo):
        """
        GitHub 최신 릴리즈 태그 조회.

        API 키 불필요: /releases/latest 는 최신 릴리즈 태그로 리다이렉트됨.
        최종 URL이 .../releases/tag/{tag} 이면 태그 반환, 릴리즈가 없으면
        /releases 로 폴백되어 None. TTL 캐시 적용.
        """
        repo_key = f"{owner}/{repo}"
        now = time.time()
        cached = _RELEASE_TAG_CACHE.get(repo_key)
        if cached and (now - cached[1]) < _RELEASE_TAG_CACHE_TTL:
            return cached[0]

        tag = None
        try:
            url = f"https://github.com/{owner}/{repo}/releases/latest"
            req = Request(url, headers={"User-Agent": "BookOasis/1.0"})
            with urlopen(req, timeout=15) as resp:
                final_url = resp.geturl()
            if final_url:
                m = re.search(r"/releases/tag/([^/?#]+)$", final_url)
                if m:
                    tag = m.group(1)
        except Exception:
            tag = None

        _RELEASE_TAG_CACHE[repo_key] = (tag, now)
        return tag

    def _fetch_gitea_latest_release_tag(self, base, owner, repo, token=None):
        """Gitea 최신 릴리즈 태그 조회 (API /api/v1/repos/{o}/{r}/releases/latest).
        릴리즈 없으면 None. 비공개 저장소는 토큰 필요."""
        api_base = "{0}/api/v1".format(base)
        url = "{0}/repos/{1}/{2}/releases/latest".format(api_base, owner, repo)
        try:
            headers = {"User-Agent": "BookOasis/1.0", "Accept": "application/json"}
            if token:
                headers["Authorization"] = "token {0}".format(token)
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            tag = str(data.get("tag_name") or "").strip()
            return tag or None
        except Exception:
            return None

    def _parse_raw_base_url(self, raw_base_url):
        """raw.githubusercontent.com URL에서 (owner, repo, branch, subpath) 추출.
        subpath: branch 이후의 서브디렉토리 경로 (없으면 '').
        e.g. .../madnite1/plugin_manager/main → ('madnite1', 'plugin_manager', 'main', '')
             .../leeyj/BookOasis_stable/main/plugins/metadata/stats_dashboard → ('leeyj', 'BookOasis_stable', 'main', 'plugins/metadata/stats_dashboard')
             .../yume-script/plugin_board/refs/heads/main → ('yume-script', 'plugin_board', 'main', '')
        Gitea raw URL: https://host/owner/repo/raw/branch/<branch>[/subpath] 도 지원.
        """
        url = str(raw_base_url or "").strip().rstrip("/")
        # Gitea: https://host/{o}/{r}/raw/branch/{branch}[/subpath] 또는 /raw/{branch}
        m = re.match(
            r"^https?://([^/]+)/([^/]+)/([^/]+)/raw/(?:branch/)?([^/]+)(/.*)?$", url
        )
        if m:
            return m.group(2), m.group(3), m.group(4), (m.group(5) or "").strip("/")
        m = re.match(
            r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)(/.*)?$", url
        )
        if m:
            owner, repo, seg3, rest = m.group(1), m.group(2), m.group(3), (m.group(4) or "")
            # refs/heads/<branch> 전체 브랜치 표기 대응 (seg3='refs', rest='/heads/<branch>[/subpath]')
            if seg3 == "refs" and rest.startswith("/heads/"):
                parts = rest.split("/")
                # parts = ['', 'heads', '<branch>', ...]
                if len(parts) >= 3:
                    return owner, repo, parts[2], "/".join(parts[3:]).strip("/")
            subpath = rest.strip("/")
            return owner, repo, seg3, subpath
        return None

    def _ensure_git_source_from_raw_base_url(self, plugin_id, raw_base_url, manifest_files):
        """소스 메타가 없을 때, raw_base_url에서 GitHub/Gitea 정보를 추론하여
        git 설치 시와 동일한 형태로 sqlite plugin_sources 에 저장한다.
        단, 서브디렉토리 경로(monorepo 내 플러그인)는 릴리즈 태그 기준이 달라
        잘못된 태그를 참조하므로 생성하지 않는다."""
        try:
            parsed = self._parse_raw_base_url(raw_base_url)
            if not parsed:
                return None
            owner, repo, branch, subpath = parsed
            if subpath:
                # 모놀리식 저장소의 서브디렉토리 — 릴리즈 태그가 플러그인 기준이 아니므로 스킵
                return None
            if "raw.githubusercontent.com" in str(raw_base_url):
                git_url = f"https://github.com/{owner}/{repo}"
            else:
                # Gitea raw — 호스트는 raw_base_url에서 추출
                host = re.sub(r"^https?://", "", str(raw_base_url)).split("/")[0]
                git_url = f"https://{host}/{owner}/{repo}"
            git_source_info = {
                "git_url": git_url,
                "branch": branch,
                "installed_at": datetime.now().isoformat(),
                "manifest_files": manifest_files or [],
            }
            self._sources_set(plugin_id, git_source_info)
            return git_source_info
        except Exception:
            return None

    def _resolve_update_base_url(self, plugin_id, raw_base_url, manifest_files=None, db_type=None):
        """업데이트 소스 URL 결정: 릴리즈 태그 우선, 없으면 브랜치(raw_base_url) 폴백.
        GitHub는 /releases/latest 리다이렉트, Gitea는 API로 태그 조회 (토큰 필요 시 사용).
        소스 메타가 없으면 raw_base_url에서 추론하여 저장한 뒤 동일하게 처리."""
        try:
            git_info = self._read_git_source_info(plugin_id)
            if not git_info:
                git_info = self._ensure_git_source_from_raw_base_url(
                    plugin_id, raw_base_url, manifest_files
                )
            git_url = (git_info.get("git_url") or "") if git_info else ""
            parsed = self._parse_git_repo(git_url)
            if not parsed:
                return raw_base_url
            branch = str(git_info.get("branch") or "").strip() or self._host_branch(parsed)
            raw_prefix = self._host_zip_base(parsed)[2]
            # 현재 raw_base_url이 이 저장소의 raw 브랜치 경로인지 확인 후 태그로 교체
            if raw_base_url.startswith(raw_prefix):
                # GitHub/Gitea 공통: 태그 조회
                if parsed["type"] == "github":
                    tag = self._fetch_latest_release_tag(parsed["owner"], parsed["repo"])
                else:
                    token = self._gitea_token_for_host(db_type, parsed["host"])
                    tag = self._fetch_gitea_latest_release_tag(
                        parsed["base"], parsed["owner"], parsed["repo"], token
                    )
                if tag:
                    # Gitea raw URL은 /raw/branch/<branch> 형식, GitHub는 /raw.githubusercontent.com/.../<branch>
                    if parsed["type"] == "gitea" and "/raw/branch/" in raw_base_url:
                        return raw_base_url.replace(
                            raw_prefix + "/branch/" + branch,
                            raw_prefix + "/branch/" + tag,
                            1,
                        )
                    return raw_base_url.replace(
                        raw_prefix + "/" + branch,
                        raw_prefix + "/" + tag,
                        1,
                    )
        except Exception:
            pass
        return raw_base_url

    def _fetch_text(self, url, timeout=15, token=None):
        """URL GET → 텍스트 (UTF-8, 오류 시 예외 전파). token은 Gitea 인증 헤더용."""
        headers = {"User-Agent": "BookOasis/1.0"}
        if token:
            headers["Authorization"] = "token {0}".format(token)
        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _parse_remote_version(self, text, version_key="plugin version"):
        """VERSION 텍스트에서 버전 문자열 추출 (JSON dict 우선, 키 정규식 폴백)"""
        if not text:
            return None
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in (version_key, "plugin version", "plugin_version", "version"):
                    val = data.get(key)
                    if val:
                        return str(val).strip()
        except Exception:
            pass
        for key in (version_key, "plugin version", "plugin_version", "version"):
            if not key:
                continue
            m = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(str(key)), text)
            if m:
                return m.group(1).strip()
        return None

    def _parse_version_tuple(self, v):
        """버전 문자열 → (major, minor, patch) tuple (v 접두사/pre-release 무시, 실패 시 None)"""
        if not v:
            return None
        m = self._VERSION_RE.match(str(v).strip())
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def _can_update_to_version(self, local_version, remote_version):
        """로컬 < 원격일 때만 업데이트 허용 (동일/다운그레이드 차단, 코어 규칙과 동일)"""
        local_t = self._parse_version_tuple(local_version)
        remote_t = self._parse_version_tuple(remote_version)
        if not local_t or not remote_t:
            return False
        return local_t < remote_t

    def _read_local_plugin_version(self, pdir, version_file, version_key):
        """로컬 VERSION 파일 읽기"""
        try:
            vpath = os.path.join(pdir, version_file)
            with open(vpath, "r", encoding="utf-8") as f:
                return self._parse_remote_version(f.read(), version_key)
        except Exception:
            return None

    def _fetch_remote_plugin_version(self, base_url, version_file="VERSION", version_key="plugin version", token=None):
        """원격 VERSION 조회 (실패/파싱 불가 시 None — 체크는 조용히 실패)
        token은 Gitea 인증용 (비공개 저장소)."""
        try:
            url = f"{base_url.rstrip('/')}/{version_file}"
            return self._parse_remote_version(self._fetch_text(url, token=token), version_key)
        except Exception:
            return None

    def _check_update_action(self, plugin_id, db_type):
        """단일 플러그인 업데이트 여부 비동기 조회 (check_update 액션)"""
        base_dir = self._get_plugins_base_dir()
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", plugin_id or ""):
            return False, f"유효하지 않은 플러그인 ID입니다: {plugin_id}"
        full_path = os.path.join(base_dir, plugin_id)
        if not os.path.isdir(full_path):
            return False, f"플러그인을 찾을 수 없습니다: {plugin_id}"

        # 로컬 버전 읽기
        version = "1.0.0"
        version_file = os.path.join(full_path, "VERSION")
        if os.path.isfile(version_file):
            try:
                with open(version_file, "r", encoding="utf-8") as f:
                    vdata = json.load(f)
                    version = vdata.get("plugin version") or vdata.get("version") or "1.0.0"
            except Exception:
                pass

        # provider 클래스 조회
        cls_obj = None
        try:
            from services.metadata_factory import MetadataFactory
            for p_name, target_cls in MetadataFactory._discover_provider_classes():
                if getattr(target_cls, 'id', p_name) == plugin_id:
                    cls_obj = target_cls
                    break
        except Exception as e:
            print(f"[PluginManager] check_update discover error: {e}")

        has_update, latest_version = self._check_plugin_update(plugin_id, version, cls_obj, db_type)
        return True, {
            "plugin_id": plugin_id,
            "version": version,
            "has_update": has_update,
            "latest_version": latest_version,
        }

    def _check_plugin_update(self, plugin_id, local_version, cls_obj, db_type=None):
        """릴리즈 태그 우선, 브랜치 폴백 업데이트 체크 (자동 업데이트는 진행하지 않음)"""
        has_update = False
        latest_version = local_version

        update_manifest = getattr(cls_obj, "update_manifest", None) if cls_obj else None
        if not (update_manifest and isinstance(update_manifest, dict) and update_manifest.get("enabled")):
            return has_update, latest_version

        try:
            spec = self._build_update_spec(plugin_id, update_manifest)
            if not spec:
                return has_update, latest_version

            base_url = self._resolve_update_base_url(
                plugin_id, spec["raw_base_url"], spec.get("files"), db_type
            )
            # Gitea 소스면 해당 호스트 토큰 사용 (비공개 저장소 인증)
            gitea_token = None
            raw_parsed = self._parse_raw_base_url(spec["raw_base_url"])
            if raw_parsed:
                src_host = re.sub(r"^https?://", "", str(spec["raw_base_url"])).split("/")[0]
                gitea_token = self._gitea_token_for_host(db_type, src_host)
            remote_ver = self._fetch_remote_plugin_version(
                base_url,
                version_file=spec["version_file"],
                version_key=spec["version_key"],
                token=gitea_token,
            )
            if remote_ver and self._can_update_to_version(local_version, remote_ver):
                return True, remote_ver
        except Exception:
            pass

        return has_update, latest_version

    def _validation_fail_response(self, source_checks, db_type, allow_invalid=None):
        """검증 실패 시 안내 메시지 + 프론트용 구조화 마커 생성 (2-튜플 유지, 코어 변경 불필요)
        allow_invalid=True  → 설정으로 허용됨: 프론트에서 위반 항목 확인 후 계속 여부 confirm (force 재시도)
        allow_invalid=False → 설정으로 차단됨: 즉시 중단, 설정에서 켜야 설치 가능
        """
        if allow_invalid is None:
            allow_invalid = self._catalog_get_allow_invalid_install(db_type)
        failed_items = [f"- {c['name']}: {c['detail']}" for c in source_checks if not c.get("ok")]
        guide_refs = []
        for c in source_checks:
            if not c.get("ok") and c.get("guide_ref"):
                ref = c["guide_ref"]
                if ref not in guide_refs:
                    guide_refs.append(ref)
        import json as _json
        payload = {
            "validation_failed": True,
            "allow_invalid_install": bool(allow_invalid),
            "checks": [
                {"name": c["name"], "ok": c.get("ok"), "detail": c.get("detail"),
                 "guide_ref": c.get("guide_ref")}
                for c in source_checks if not c.get("ok")
            ],
            "guide_refs": guide_refs,
        }
        if allow_invalid:
            msg = (
                "플러그인 검증 실패 — 아래 가이드 규정 위반 항목을 확인하세요 (설정에서 '검증 실패시 설치 가능'이 켜져 있습니다):\n"
                + "\n".join(failed_items)
                + ("\n참조: " + "\n".join(guide_refs) if guide_refs else "")
                + "\n계속 설치하려면 아래 확인 버튼을 누르세요. (위험)"
                + "\n__VALIDATION_FAILED__" + _json.dumps(payload, ensure_ascii=False)
            )
        else:
            msg = (
                "플러그인 검증 실패 — 설치를 중단했습니다 (기존 폴더는 변경되지 않음):\n"
                + "\n".join(failed_items)
                + ("\n참조: " + "\n".join(guide_refs) if guide_refs else "")
                + "\n설치하려면 플러그인 매니저 설정에서 '검증 실패시 설치 가능'을 켠 후 다시 시도하세요."
            )
        return False, msg

    def _install_from_zip(self, zip_data_b64, filename, db_type, force=False):
        """Zip 압축 파일 업로드를 통한 플러그인 설치
        force=True: 1차 정적 검증 실패 시에도 경고만 하고 설치 계속 (설정/사용자 확인 후).
        """
        if not zip_data_b64:
            return False, "압축 파일 데이터가 누락되었습니다."

        import base64
        import zipfile
        import io

        temp_dir = tempfile.mkdtemp(prefix="bo_plugin_zip_")

        try:
            # Base64 데이터 추출
            if "," in zip_data_b64:
                zip_data_b64 = zip_data_b64.split(",", 1)[1]

            zip_bytes = base64.b64decode(zip_data_b64)
            zip_file = zipfile.ZipFile(io.BytesIO(zip_bytes))

            # 압축 해제 경로 이탈 검사 (Zip Slip 차단)
            for member in zip_file.namelist():
                filename_clean = os.path.normpath(member)
                if filename_clean.startswith("..") or filename_clean.startswith("/") or filename_clean.startswith("\\"):
                    return False, f"보안 경고: 압축 파일 내 유효하지 않은 상위 경로가 포함되어 있습니다: {member}"

            zip_file.extractall(temp_dir)

            # 플러그인 루트 디렉토리 지능형 탐색 (중첩 폴더 및 __MACOSX 방지)
            target_plugin_dir = self._find_plugin_root_dir(temp_dir)
            plugin_id = self._detect_plugin_id(target_plugin_dir, fallback_name=filename)
            if not plugin_id:
                return False, "플러그인 ID를 식별할 수 없습니다. (BaseMetadataProvider 클래스 또는 VERSION 파일 필요)"

            # 안전성 검증
            if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
                return False, f"유효하지 않은 플러그인 ID입니다 (영문/숫자/언더바/하이픈만 허용): {plugin_id}"

            if plugin_id in ("base.py", "base", "__pycache__", "plugin_manager"):
                return False, "시스템 예약어 또는 핵심 플러그인은 덮어쓸 수 없습니다."

            # 1차 검증: 정적 소스 검증 (코드 실행 없음 — AST/파일 스캔)
            source_ok, source_checks = self._validate_plugin_source(target_plugin_dir, plugin_id)
            if not source_ok and not force:
                return self._validation_fail_response(source_checks, db_type)

            dest_dir, err = self._validate_plugin_path(plugin_id)
            if err or not dest_dir:
                return False, err or "유효하지 않은 플러그인 경로입니다."

            # 이전 디렉토리 존재 시 교체
            if os.path.exists(dest_dir):
                shutil.rmtree(dest_dir)

            # 디렉토리 복사 (.git 및 macOS 쓰레기 파일 제외)
            shutil.copytree(
                target_plugin_dir,
                dest_dir,
                ignore=shutil.ignore_patterns(".git", ".github", "__pycache__", "*.pyc", "__MACOSX", ".DS_Store")
            )

            # 소스 메타 저장: 설치 방식과 무관하게 update_manifest의 raw_base_url을
            # 검증해 판단한다 — 유효한 GitHub 루트면 git_url 저장(업데이트/배지 활성),
            # 없거나 monorepo 서브디렉토리면 레코드 없음(로컬 플러그인 유지).
            try:
                files_clean, manifest = self._extract_update_manifest_files(dest_dir)
                raw_base_url = str((manifest or {}).get("raw_base_url") or "").strip().rstrip("/")
                if files_clean and raw_base_url:
                    self._ensure_git_source_from_raw_base_url(plugin_id, raw_base_url, files_clean)
            except Exception:
                pass

            self.get_db_gateway('general').set_setting(f"PLUGIN_ENABLED_{plugin_id}", "1")

            # Hot reload
            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)

            # 2차 검증: 실제 플러그인 로드 확인 (실패 시 설치 폴더 삭제)
            loaded_ok = False
            try:
                providers = MetadataFactory.get_available_providers()
                loaded_ok = any(str(p.get("id")) == plugin_id for p in providers)
            except Exception as e:
                logger.warning("플러그인 로드 검증 실패 (id=%s): %s", plugin_id, e)

            if not loaded_ok:
                if os.path.exists(dest_dir):
                    shutil.rmtree(dest_dir, ignore_errors=True)
                return False, (
                    f"검증 실패: '{plugin_id}' 플러그인이 설치 후 로드되지 않았습니다. "
                    f"(클래스 id와 폴더명이 일치하는지 확인 필요) — 설치 폴더를 삭제했습니다."
                )

            passed = [c["name"] for c in source_checks if c.get("ok") and not c.get("warn")]
            warns = [c["detail"] for c in source_checks if c.get("warn")]
            result_msg = (
                f"ZIP 압축 파일을 통해 '{plugin_id}' 플러그인이 성공적으로 설치 및 활성화되었습니다! "
                f"(검증 통과: {', '.join(passed)})"
            )
            if force:
                result_msg += " [경고] 검증 실패 항목을 무시하고 설치했습니다."
            if warns:
                result_msg += " 경고: " + "; ".join(warns)
            return True, result_msg

        except zipfile.BadZipFile:
            return False, "올바른 ZIP 압축 파일 형식이 아닙니다."
        except Exception as e:
            return False, f"ZIP 플러그인 설치 중 오류가 발생했습니다: {str(e)}"
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _install_from_git(self, git_url, db_type, force=False):
        """
        GitHub/Gitea 저장소 URL 을 통한 플러그인 설치 (git 바이너리 불필요).
        force=True: 1차 정적 검증 실패 시에도 경고만 하고 설치 계속 (설정/사용자 확인 후).

        절차:
          1. 저장소 소스를 HTTP ZIP 으로 다운로드 (urllib 표준 라이브러리만 사용)
          2. 다운로드된 코드에서 update_manifest 를 AST 로 안전하게 추출
          3. update_manifest.files 목록에 있는 파일만 남기고 전부 삭제
          4. plugins/metadata/<plugin_id> 로 복사 후 활성화 + 핫 리로드
        """
        git_url = str(git_url or "").strip()
        if not git_url:
            return False, "Git 저장소 URL이 누락되었습니다."

        # URL scheme 안전성 검증 (http/https 만 허용 — git 바이너리 의존 없음)
        if not re.match(r'^https?://', git_url, re.IGNORECASE):
            return False, "지원하지 않는 Git URL 형식입니다. (http/https URL만 허용)"

        zip_url, branch = self._build_repo_zip_url(git_url)
        if not zip_url:
            return False, "Git 저장소 URL 형식을 인식할 수 없습니다."

        temp_dir = tempfile.mkdtemp(prefix="bo_plugin_git_")

        try:
            import io
            import zipfile

            # 1. ZIP 다운로드 (릴리즈 태그 우선 → 기본 브랜치 main → 실패 시 master 폴백)
            #    GitHub 소스 + 브랜치 미지정(/tree/ 또는 /src/branch/ 없음)이면
            #    최신 릴리즈 태그 ZIP을 최우선 후보로 사용 — 설치와 업데이트 소스 일치.
            candidates = list(self._zip_url_candidates(zip_url))
            release_zip_url = None
            release_tag = None
            if "/tree/" not in git_url and "/src/branch/" not in git_url:
                repo = self._parse_github_repo(git_url)
                if repo:
                    release_tag = self._fetch_latest_release_tag(repo[0], repo[1])
                    if release_tag:
                        release_zip_url = (
                            f"https://codeload.github.com/{repo[0]}/{repo[1]}"
                            f"/zip/refs/tags/{release_tag}"
                        )
                        if release_zip_url not in candidates:
                            candidates.insert(0, release_zip_url)

            zip_bytes = None
            last_err = None
            used_url = None
            for cand_url in candidates:
                try:
                    req = Request(cand_url, headers={"User-Agent": "BookOasis/1.0"})
                    with urlopen(req, timeout=60) as resp:
                        zip_bytes = resp.read()
                    used_url = cand_url
                    break
                except HTTPError as e:
                    last_err = f"{e.code} {e.reason}"
                except Exception as e:
                    last_err = str(e)
            if not zip_bytes:
                return False, f"저장소 ZIP 다운로드 실패: {last_err}"

            # 릴리즈 태그로 설치한 경우 branch 메타를 태그명으로 기록 (업데이트 엔진 소스와 일치)
            if release_zip_url and used_url == release_zip_url:
                branch = release_tag

            # 2. 압축 해제 (Zip Slip 차단)
            zip_file = zipfile.ZipFile(io.BytesIO(zip_bytes))
            for member in zip_file.namelist():
                member_clean = os.path.normpath(member)
                if (member_clean.startswith("..") or member_clean.startswith("/")
                        or member_clean.startswith("\\")):
                    return False, f"보안 경고: 압축 파일 내 유효하지 않은 상위 경로가 포함되어 있습니다: {member}"
            zip_file.extractall(temp_dir)

            # 3. 플러그인 루트 탐색 (단독 플러그인 저장소 가정: 루트가 플러그인 자체)
            target_plugin_dir = self._find_plugin_root_dir(temp_dir)
            if not target_plugin_dir:
                return False, "다운로드된 저장소에서 플러그인 디렉토리를 찾을 수 없습니다."

            # 4. update_manifest.files 추출 + 플러그인 ID 감지
            manifest_files, manifest = self._extract_update_manifest_files(target_plugin_dir)
            has_manifest = bool(manifest_files)
            manifest_files = manifest_files or []  # 폴백 진행 시 None 방지
            if not has_manifest:
                # update_manifest 없는 저장소 → "검증 실패시 설치 가능" 옵션 게이트
                # (구조 실패가 아닌 옵션 우회 구간으로 취급 — ON 시에만 폴백 진행)
                allow_invalid = self._catalog_get_allow_invalid_install(db_type)
                if not allow_invalid:
                    return False, (
                        "다운로드된 저장소에서 update_manifest 를 찾을 수 없습니다. "
                        "이 저장소는 자동 업데이트 계약(update_manifest)이 없는 저장소입니다. "
                        "설치하려면 플러그인 매니저 설정에서 '검증 실패시 설치 가능'을 켠 후 다시 시도하세요."
                    )
                if not force:
                    # 옵션 ON + 최초 시도 → 프론트 confirm 유도 (기존 __VALIDATION_FAILED__ 마커 재사용)
                    import json as _json
                    return False, (
                        "이 저장소는 update_manifest 가 없어 자동 업데이트가 불가합니다. "
                        "그래도 설치할까요? (전체 파일이 플러그인 폴더로 복사됩니다)\n"
                        "__VALIDATION_FAILED__" + _json.dumps({
                            "validation_failed": True,
                            "allow_invalid_install": True,
                            "checks": [{
                                "name": "update_manifest 선언",
                                "ok": False,
                                "detail": "update_manifest 가 없습니다. 업데이트/배지가 비활성화됩니다.",
                                "guide_ref": "§3.1",
                            }],
                            "guide_refs": ["§3.1"],
                        }, ensure_ascii=False)
                    )
                # force=True (사용자 confirm 통과) → 아래 폴백 경로로 진행 (전체 복사 설치)
            plugin_id = self._detect_plugin_id(target_plugin_dir)
            if not plugin_id:
                return False, "플러그인 ID를 식별할 수 없습니다. (BaseMetadataProvider 클래스 또는 VERSION 파일 필요)"

            if not re.match(r'^[a-zA-Z0-9_-]+$', plugin_id):
                return False, f"유효하지 않은 플러그인 ID입니다 (영문/숫자/언더바/하이픈만 허용): {plugin_id}"

            if plugin_id in ("base.py", "base", "__pycache__", "plugin_manager"):
                return False, "시스템 예약어 또는 핵심 플러그인은 덮어쓸 수 없습니다."

            dest_dir, err = self._validate_plugin_path(plugin_id)
            if err or not dest_dir:
                return False, err or "유효하지 않은 플러그인 경로입니다."

            # 5. files 목록 경로 안전성 검증 (경로 이탈 차단 — manifest 있을 때만)
            for rel in manifest_files:
                rel_clean = os.path.normpath(str(rel))
                if (rel_clean.startswith("..") or rel_clean.startswith("/")
                        or rel_clean.startswith("\\") or rel_clean in (".", "")):
                    return False, f"update_manifest 에 유효하지 않은 파일 경로가 포함되어 있습니다: {rel}"

            # 5-1. 1차 검증: 정적 소스 검증 (코드 실행 없음 — AST/파일 스캔, zip 설치와 동일 기준)
            #      prune 전에 수행해야 UI 번들/VERSION/symlink 등 전체 파일 기준 검사 가능
            source_ok, source_checks = self._validate_plugin_source(target_plugin_dir, plugin_id)
            if not source_ok and not force:
                return self._validation_fail_response(source_checks, db_type)

            # 6. manifest 있을 때만 목록 외 전부 삭제 (.git 등 포함 안전 처리)
            #    manifest 없음(폴백) → 전체 복사, prune 스킵 (빈 목록이면 전부 삭제 위험)
            if has_manifest:
                try:
                    self._prune_plugin_dir(target_plugin_dir, manifest_files)
                except Exception as e:
                    return False, f"플러그인 파일 정리 중 오류가 발생했습니다: {str(e)}"

            # 7. 이전 디렉토리 교체 후 복사 (manifest 없음 → ZIP 방식 ignore 패턴으로 전체 복사)
            if os.path.exists(dest_dir):
                shutil.rmtree(dest_dir)
            if has_manifest:
                shutil.copytree(target_plugin_dir, dest_dir)
            else:
                shutil.copytree(
                    target_plugin_dir, dest_dir,
                    ignore=shutil.ignore_patterns(
                        ".git", ".github", "__pycache__", "*.pyc", "__MACOSX", ".DS_Store"
                    )
                )

            # 8. Git 소스 메타 정보 저장 (sqlite plugin_sources — .git_source 파일 미생성)
            git_source_info = {
                "git_url": git_url,
                "branch": branch,
                "installed_at": datetime.now().isoformat(),
                "manifest_files": manifest_files,
            }
            self._sources_set(plugin_id, git_source_info)

            # 9. 활성화 + 핫 리로드
            self.get_db_gateway('general').set_setting(f"PLUGIN_ENABLED_{plugin_id}", "1")

            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)

            # 2차 검증: 실제 플러그인 로드 확인 (실패 시 설치 폴더 삭제 — zip 설치와 동일 기준)
            loaded_ok = False
            try:
                providers = MetadataFactory.get_available_providers()
                loaded_ok = any(str(p.get("id")) == plugin_id for p in providers)
            except Exception as e:
                logger.warning("플러그인 로드 검증 실패 (id=%s): %s", plugin_id, e)

            if not loaded_ok:
                if os.path.exists(dest_dir):
                    shutil.rmtree(dest_dir, ignore_errors=True)
                return False, (
                    f"검증 실패: '{plugin_id}' 플러그인이 설치 후 로드되지 않았습니다. "
                    f"(클래스 id와 폴더명이 일치하는지 확인 필요) — 설치 폴더를 삭제했습니다."
                )

            passed = [c["name"] for c in source_checks if c.get("ok") and not c.get("warn")]
            warns = [c["detail"] for c in source_checks if c.get("warn")]
            source_label = (
                f"릴리즈 태그 {release_tag}"
                if (release_zip_url and used_url == release_zip_url)
                else f"브랜치 {branch}"
            )
            result_msg = (
                f"Git 저장소({source_label})에서 '{plugin_id}' 플러그인이 성공적으로 설치 및 활성화되었습니다! "
            )
            if has_manifest:
                result_msg += (
                    f"(update_manifest 기준 {len(manifest_files)}개 파일만 유지, 검증 통과: {', '.join(passed)})"
                )
            else:
                result_msg += (
                    "(update_manifest 가 없는 저장소 — 전체 파일이 복사되었으며, 자동 업데이트/업데이트 버튼이 비활성화됩니다)"
                )
            if force:
                result_msg += " [경고] 검증 실패 항목을 무시하고 설치했습니다."
            if warns:
                result_msg += " 경고: " + "; ".join(warns)
            return True, result_msg

        except Exception as e:
            return False, f"Git 플러그인 설치 중 오류가 발생했습니다: {str(e)}"
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _build_repo_zip_url(self, git_url):
        """
        저장소 웹 URL → 소스 ZIP 다운로드 URL 변환.

        - GitHub:  https://github.com/owner/repo[/tree/branch]
                  → https://codeload.github.com/owner/repo/zip/refs/heads/{branch}
        - Gitea 등: https://host/org/repo[/src/branch/branch]
                  → https://host/org/repo/archive/{branch}.zip
        반환: (zip_url, branch) 또는 (None, None)
        """
        url = str(git_url or "").strip().rstrip("/")
        url = re.sub(r'\.git$', '', url)

        # GitHub
        m = re.match(r'^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:/tree/([^/]+))?$', url)
        if m:
            owner, repo, branch = m.group(1), m.group(2), (m.group(3) or "main")
            return f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}", branch

        # GitHub .git 형식 (브랜치 지정 불가)
        m = re.match(r'^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)$', url)
        if m:
            owner, repo = m.group(1), m.group(2)
            return f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main", "main"

        # Gitea/GitLab 등 (archive 방식)
        m = re.match(r'^https?://([^/]+)/([^/]+)/([^/]+?)(?:/src/branch/([^/]+))?$', url)
        if m:
            host, org, repo_name, branch = m.group(1), m.group(2), m.group(3), (m.group(4) or "main")
            return f"https://{host}/{org}/{repo_name}/archive/{branch}.zip", branch

        return None, None

    def _zip_url_candidates(self, zip_url):
        """기본 브랜치 실패 시 master 폴백 후보 URL 목록 생성"""
        candidates = [zip_url]
        if "/refs/heads/main" in zip_url:
            candidates.append(zip_url.replace("/refs/heads/main", "/refs/heads/master"))
        elif "/archive/main.zip" in zip_url:
            candidates.append(zip_url.replace("/archive/main.zip", "/archive/master.zip"))
        return candidates

    def _extract_update_manifest_files(self, plugin_dir):
        """
        플러그인 .py 소스에서 update_manifest dict 를 AST 로 안전하게 추출.

        코드를 실행하지 않고 리터럴 dict 만 읽으므로 악성 코드 실행 위험이 없습니다.
        반환: (files 리스트, manifest dict) 또는 (None, None)
        """
        try:
            for fname in sorted(os.listdir(plugin_dir)):
                if not fname.endswith(".py") or fname in ("__init__.py", "base.py"):
                    continue
                fpath = os.path.join(plugin_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        tree = ast.parse(f.read(), filename=fpath)
                except SyntaxError:
                    continue

                for node in ast.walk(tree):
                    if not isinstance(node, ast.ClassDef):
                        continue
                    for stmt in node.body:
                        value_node = None
                        if isinstance(stmt, ast.Assign):
                            for target in stmt.targets:
                                if isinstance(target, ast.Name) and target.id == "update_manifest":
                                    value_node = stmt.value
                                    break
                        elif (isinstance(stmt, ast.AnnAssign)
                              and isinstance(stmt.target, ast.Name)
                              and stmt.target.id == "update_manifest"):
                            value_node = stmt.value

                        if value_node is None:
                            continue
                        try:
                            value = ast.literal_eval(value_node)
                        except Exception:
                            value = None
                        if not isinstance(value, dict):
                            continue
                        raw_files = value.get("files")
                        if isinstance(raw_files, list):
                            files_clean = [str(x).strip() for x in raw_files if str(x).strip()]
                            if files_clean:
                                return files_clean, value
        except Exception:
            pass
        return None, None

    def _prune_plugin_dir(self, plugin_dir, keep_files):
        """
        keep_files 목록에 있는 파일/디렉토리만 남기고 전부 삭제.

        - .git 디렉토리는 무조건 제거 (목록 포함 여부와 무관)
        - keep_files 에 디렉토리 경로가 있으면 해당 디렉토리 트리 전체 유지
        - 나머지 파일/디렉토리/숨김 파일은 삭제
        """
        keep_set = set()
        for rel in keep_files:
            rel_clean = os.path.normpath(str(rel)).lstrip("./").lstrip("/")
            if rel_clean:
                keep_set.add(rel_clean)

        for root, dirs, files in os.walk(plugin_dir, topdown=True):
            rel_root = os.path.relpath(root, plugin_dir)
            rel_root_clean = "" if rel_root == "." else rel_root

            keep_dirs = []
            for d in dirs:
                d_rel = os.path.normpath(os.path.join(rel_root_clean, d)) if rel_root_clean else d
                if d == ".git":
                    # .git 은 항상 제거
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    continue
                # 이 디렉토리 하위에 유지 대상 파일/디렉토리가 있는 경우에만 유지
                if any(k == d_rel or k.startswith(d_rel + os.sep) for k in keep_set):
                    keep_dirs.append(d)
                else:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
            dirs[:] = keep_dirs

            for name in files:
                f_rel = os.path.normpath(os.path.join(rel_root_clean, name)) if rel_root_clean else name
                if f_rel not in keep_set:
                    os.remove(os.path.join(root, name))

    def _find_plugin_root_dir(self, start_dir):
        """압축 해제된 폴더 내에서 메타데이터 플러그인 루트 디렉토리를 깊이 탐색"""
        if self._is_plugin_directory(start_dir):
            return start_dir

        for root, dirs, files in os.walk(start_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__MACOSX"]
            if self._is_plugin_directory(root):
                return root

        subdirs = [os.path.join(start_dir, d) for d in os.listdir(start_dir)
                   if os.path.isdir(os.path.join(start_dir, d)) and not d.startswith(".") and d != "__MACOSX"]
        if len(subdirs) == 1:
            return subdirs[0]

        return start_dir

    def _is_plugin_directory(self, dpath):
        """디렉토리가 유효한 플러그인 구성 요소들을 포함하고 있는지 판별"""
        if not os.path.isdir(dpath):
            return False

        if os.path.isfile(os.path.join(dpath, "VERSION")):
            return True

        try:
            for f in os.listdir(dpath):
                if f.endswith(".py") and f not in ("base.py", "__init__.py"):
                    fpath = os.path.join(dpath, f)
                    if not os.path.isfile(fpath):
                        continue
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as py_f:
                            tree = ast.parse(py_f.read(), filename=fpath)
                    except SyntaxError:
                        continue
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.ClassDef):
                            continue
                        # BaseMetadataProvider 상속 클래스 또는 id 속성을 가진 클래스 = 플러그인 후보
                        is_provider = any("BaseMetadataProvider" in ast.unparse(b)
                                          for b in node.bases)
                        has_id_attr = any(
                            (isinstance(stmt, ast.Assign) and any(
                                isinstance(t, ast.Name) and t.id == "id"
                                for t in stmt.targets))
                            or (isinstance(stmt, ast.AnnAssign)
                                and isinstance(stmt.target, ast.Name)
                                and stmt.target.id == "id")
                            for stmt in node.body
                        )
                        if is_provider or has_id_attr:
                            return True
        except Exception:
            pass

        return False

    def _detect_plugin_id(self, plugin_dir, fallback_name=None):
        """디렉토리 내 파일에서 plugin_id 자동 감지"""
        # 1. VERSION 파일 내 정보 확인
        vpath = os.path.join(plugin_dir, "VERSION")
        if os.path.isfile(vpath):
            try:
                with open(vpath, "r", encoding="utf-8") as f:
                    vdata = json.load(f)
                    p_id = vdata.get("id") or vdata.get("plugin_id")
                    if p_id and re.match(r'^[a-zA-Z0-9_-]+$', str(p_id).strip()):
                        return str(p_id).strip()
            except Exception:
                pass

        # 2. Python 코드에서 id = "..." 검색 (AST 기반 — docstring/주석/문자열 내
        #    `id="..."` 패턴(예: HTML script 태그, JSON 예시)을 실제 클래스 속성으로
        #    오인하지 않도록 실제 클래스 본문의 id 속성만 추출)
        try:
            for fname in os.listdir(plugin_dir):
                if fname.endswith(".py") and fname not in ("__init__.py", "base.py"):
                    fpath = os.path.join(plugin_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            tree = ast.parse(f.read(), filename=fpath)
                    except SyntaxError:
                        continue
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.ClassDef):
                            continue
                        for stmt in node.body:
                            value_node = None
                            if isinstance(stmt, ast.Assign):
                                for target in stmt.targets:
                                    if isinstance(target, ast.Name) and target.id == "id":
                                        value_node = stmt.value
                                        break
                            elif (isinstance(stmt, ast.AnnAssign)
                                  and isinstance(stmt.target, ast.Name)
                                  and stmt.target.id == "id"):
                                value_node = stmt.value
                            if value_node is None:
                                continue
                            try:
                                value = ast.literal_eval(value_node)
                            except Exception:
                                continue
                            if isinstance(value, str):
                                p_id = value.strip()
                                if p_id and re.match(r'^[a-zA-Z0-9_-]+$', p_id):
                                    return p_id
        except Exception:
            pass

        # 3. 폴더 이름 또는 fallback 파일명 반환
        folder_name = os.path.basename(os.path.normpath(plugin_dir))
        if folder_name and folder_name not in ("temp", "tmp") and not folder_name.startswith("bo_plugin_zip"):
            return folder_name

        if fallback_name:
            clean_name = re.sub(r'\.zip$', '', str(fallback_name), flags=re.IGNORECASE)
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_name).strip('_')
            if clean_name:
                return clean_name

        return ""

    def _validate_plugin_source(self, plugin_dir, detected_id):
        """
        설치 대상 플러그인 소스 정적 검증 (코드 실행 없음 — AST/파일 스캔만).
        개발 가이드(guide_plugins.md) 규격 기반.

        검증 항목:
          1. VERSION: update_manifest 선언 시 필수 (JSON + 'plugin version' 키).
             미선언 시 VERSION 없음/비표준은 경고만 (업데이트 미지원 플러그인 허용)
          2. 메인 .py 존재 (BaseMetadataProvider 상속 클래스)
          3. 클래스 id == 감지된 plugin_id (폴더명과 일치해야 목록/카테고리 표시)
          4. 필수 클래스 필드 (가이드 필수/권장): name(str), is_searchable(bool), config_schema(list)
          5. 필수 메서드 search/apply 구현 (AST 클래스 본문 검사)
          6. 금지 패턴 없음 (eval/exec/subprocess/os.system/os.popen/shell=True)
          7. 심볼릭 링크 없음 (가이드 보안 규정 — 외부 경로 접근 차단)
          8. update_manifest 규격: provider=='github-raw' + version_file/version_key/files
             존재 + raw_base_url 비어있지 않음 + files 실제 존재
          9. category_tab 선언 시 UI 번들(index.html/script.js/style.css) 필수
             (가이드: 카테고리 레벨 플러그인 = 풀페이지 UI 제공 계약)
             미선언 시 부분 번들은 경고만

        반환: (성공 여부, 체크 결과 리스트 [{'name', 'ok', 'detail'}])
        """
        if os.path.basename(os.path.normpath(plugin_dir)) == "__pycache__":
            return False, []

        checks = []
        base_names = ("base.py", "__init__.py")

        # manifest 사전 추출 (VERSION 필수 판정에 사용)
        manifest_files, manifest = self._extract_update_manifest_files(plugin_dir)

        # 1. VERSION 파일 검사 (update_manifest 선언 시 필수, 미선언 시 경고만)
        vpath = os.path.join(plugin_dir, "VERSION")
        vfile_ok = False
        vdetail = ""
        if os.path.isfile(vpath):
            try:
                with open(vpath, "r", encoding="utf-8") as f:
                    vdata = json.load(f)
                vkey = vdata.get("plugin version") or vdata.get("version")
                if vkey:
                    vfile_ok = True
                    vdetail = "버전 %s" % vkey
                else:
                    vdetail = "'plugin version' 키가 없습니다 (업데이트 체크 불가)"
            except Exception:
                vdetail = "VERSION 형식이 표준 JSON이 아닙니다 (업데이트 체크 불가)"
        else:
            vdetail = "VERSION 파일 없음"

        if manifest_files:
            if vfile_ok:
                checks.append({"name": "VERSION", "ok": True, "detail": vdetail,
                               "guide_ref": "가이드 §2 디렉토리 구조 (VERSION 필수) / §7 릴리즈 절차"})
            else:
                checks.append({"name": "VERSION", "ok": False,
                               "detail": "update_manifest 선언 시 VERSION 필수 — " + vdetail,
                               "guide_ref": "가이드 §2 디렉토리 구조 (VERSION 필수) / §7 릴리즈 절차"})
        elif vfile_ok:
            checks.append({"name": "VERSION", "ok": True, "detail": vdetail,
                           "guide_ref": "가이드 §2 디렉토리 구조 (VERSION 필수) / §7 릴리즈 절차"})
        else:
            checks.append({"name": "VERSION", "ok": True, "warn": True,
                           "detail": "경고: " + vdetail + " (업데이트 체크 불가)",
                           "guide_ref": "가이드 §2 디렉토리 구조 (VERSION 필수) / §7 릴리즈 절차"})

        # 2~6. 파이썬 소스 AST 분석
        py_files = []
        try:
            py_files = [f for f in sorted(os.listdir(plugin_dir))
                        if f.endswith(".py") and f not in base_names
                        and os.path.isfile(os.path.join(plugin_dir, f))]
        except Exception:
            pass

        provider_found = False
        class_id = None
        cls_attrs = set()   # provider 클래스 본문에서 수집한 필드명 (리터럴 타입 검증 완료)
        has_search = False
        has_apply = False
        forbidden_hits = []

        for fname in py_files:
            fpath = os.path.join(plugin_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                tree = ast.parse(content, filename=fpath)
            except SyntaxError:
                forbidden_hits.append(f"{fname}: 파이썬 구문 오류")
                continue

            # 금지 패턴 검사 (AST 기반 — 주석/문자열 언급은 무시, 실제 호출/import만 차단)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name) and fn.id in ("eval", "exec"):
                        forbidden_hits.append(f"{fname}: {fn.id}() 호출 발견")
                    elif (isinstance(fn, ast.Attribute)
                          and fn.attr in ("system", "popen")):
                        forbidden_hits.append(f"{fname}: os.{fn.attr}() 호출 발견")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name == "subprocess" or a.name.startswith("subprocess."):
                            forbidden_hits.append(f"{fname}: subprocess import 발견")
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "subprocess":
                        forbidden_hits.append(f"{fname}: subprocess import 발견")
                elif isinstance(node, ast.keyword) and node.arg == "shell":
                    try:
                        if ast.literal_eval(node.value) is True:
                            forbidden_hits.append(f"{fname}: shell=True 사용")
                    except Exception:
                        pass

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = []
                for b in node.bases:
                    try:
                        bases.append(ast.unparse(b))
                    except Exception:
                        bases.append("")

                # 클래스 본문에서 필수/선택 필드 + 메서드 수집 (리터럴 타입 검증)
                cls_id = None
                cls_fields = set()
                cls_search = False
                cls_apply = False
                for stmt in node.body:
                    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                        for t in targets:
                            if not isinstance(t, ast.Name):
                                continue
                            val = stmt.value
                            if t.id == "id":
                                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                                    cls_id = val.value
                            elif t.id == "name":
                                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                                    cls_fields.add("name")
                            elif t.id == "is_searchable":
                                if isinstance(val, ast.Constant) and isinstance(val.value, bool):
                                    cls_fields.add("is_searchable")
                            elif t.id == "config_schema":
                                if isinstance(val, (ast.List, ast.Tuple)):
                                    cls_fields.add("config_schema")
                            elif t.id in ("category_tab", "update_manifest", "dashboard_widget"):
                                if isinstance(val, ast.Dict):
                                    cls_fields.add(t.id)
                    elif isinstance(stmt, ast.FunctionDef):
                        if stmt.name == "search":
                            cls_search = True
                        elif stmt.name == "apply":
                            cls_apply = True

                # BaseMetadataProvider 직접 상속 또는 id 속성을 가진 클래스 = provider 후보
                is_provider = any("BaseMetadataProvider" in b for b in bases)
                if not is_provider and cls_id is not None:
                    is_provider = True

                if is_provider and cls_id is not None and class_id is None:
                    class_id = cls_id

                if is_provider:
                    provider_found = True
                    cls_attrs.update(cls_fields)
                    if cls_search:
                        has_search = True
                    if cls_apply:
                        has_apply = True

        if not py_files:
            checks.append({"name": "소스", "ok": False, "detail": "메인 .py 파일이 없습니다",
                           "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (BaseMetadataProvider 상속)"})
        elif not provider_found:
            checks.append({"name": "소스", "ok": False,
                           "detail": "BaseMetadataProvider 상속 클래스를 찾을 수 없습니다",
                           "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (BaseMetadataProvider 상속)"})
        else:
            checks.append({"name": "소스", "ok": True, "detail": "%d개 .py 파일, BaseMetadataProvider 클래스 발견" % len(py_files),
                           "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (BaseMetadataProvider 상속)"})

        # 3. 클래스 id 일치 검사 (폴더명/감지 id와 일치해야 목록 표시)
        if class_id is not None:
            if str(class_id).strip() == str(detected_id).strip():
                checks.append({"name": "클래스 id", "ok": True, "detail": class_id,
                               "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (id 필드)"})
            else:
                checks.append({"name": "클래스 id", "ok": False,
                               "detail": f"코드 내 id='{class_id}' ≠ 감지된 id='{detected_id}' — 설치 후 목록에 표시되지 않을 수 있습니다",
                               "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (id 필드)"})
        elif provider_found:
            checks.append({"name": "클래스 id", "ok": False,
                           "detail": "플러그인 클래스에 id 속성이 없습니다",
                           "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (id 필드)"})
        else:
            checks.append({"name": "클래스 id", "ok": False, "detail": "클래스를 찾을 수 없어 검사 불가",
                           "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (id 필드)"})

        # 4. 필수 클래스 필드 검사 (가이드: id/name/is_searchable/config_schema)
        if provider_found:
            missing_fields = [f for f in ("name", "is_searchable", "config_schema")
                              if f not in cls_attrs]
            if missing_fields:
                checks.append({"name": "필수 필드", "ok": False,
                               "detail": "클래스에 없음: " + ", ".join(missing_fields),
                               "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (필수/권장 필드)"})
            else:
                checks.append({"name": "필수 필드", "ok": True,
                               "detail": "name/is_searchable/config_schema 확인",
                               "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (필수/권장 필드)"})
        else:
            checks.append({"name": "필수 필드", "ok": False, "detail": "클래스 없음"})

        # 5. 필수 메서드 검사
        if provider_found:
            missing = []
            if not has_search:
                missing.append("search")
            if not has_apply:
                missing.append("apply")
            if missing:
                checks.append({"name": "필수 메서드", "ok": False,
                               "detail": "구현 안 됨: " + ", ".join(missing),
                               "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (search/apply 메서드)"})
            else:
                checks.append({"name": "필수 메서드", "ok": True, "detail": "search/apply 확인",
                               "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (search/apply 메서드)"})
        else:
            checks.append({"name": "필수 메서드", "ok": False, "detail": "클래스 없음",
                           "guide_ref": "가이드 §3 플러그인 클래스 기본 계약 (search/apply 메서드)"})

        # 6. 금지 패턴 검사
        if forbidden_hits:
            checks.append({"name": "금지 패턴", "ok": False,
                           "detail": "; ".join(forbidden_hits[:3]),
                           "guide_ref": "가이드 §1 핵심 원칙 + §2.1 보안 제약 (코드 실행 패턴 금지)"})
        else:
            checks.append({"name": "금지 패턴", "ok": True, "detail": "eval/exec/subprocess 없음",
                           "guide_ref": "가이드 §1 핵심 원칙 + §2.1 보안 제약 (코드 실행 패턴 금지)"})

        # __init__.py 존재 여부 (없으면 폴백 로드 — 경고만)
        if os.path.isfile(os.path.join(plugin_dir, "__init__.py")):
            checks.append({"name": "__init__.py", "ok": True, "detail": "확인",
                           "guide_ref": "가이드 §2 디렉토리 구조 (__init__.py)"})
        else:
            checks.append({"name": "__init__.py", "ok": True, "warn": True,
                           "detail": "경고: __init__.py 없음 (폴백 로드 사용)",
                           "guide_ref": "가이드 §2 디렉토리 구조 (__init__.py)"})

        # 7. 심볼릭 링크 검사 (가이드 보안 규정 — 외부 경로 접근 차단)
        symlinks = []
        try:
            for root_dir, dirs, files in os.walk(plugin_dir):
                for entry in dirs + files:
                    p = os.path.join(root_dir, entry)
                    if os.path.islink(p):
                        symlinks.append(os.path.relpath(p, plugin_dir))
        except Exception:
            pass
        if symlinks:
            checks.append({"name": "심볼릭 링크", "ok": False,
                           "detail": "플러그인 폴더 내 심볼릭 링크 금지: " + ", ".join(symlinks[:3]),
                           "guide_ref": "가이드 §2.1 보안 제약 (외부 심볼릭 링크 접근 차단)"})
        else:
            checks.append({"name": "심볼릭 링크", "ok": True, "detail": "없음",
                           "guide_ref": "가이드 §2.1 보안 제약 (외부 심볼릭 링크 접근 차단)"})

        # 8. update_manifest 규격 검사 (선언된 경우에만)
        if manifest_files:
            problems = []
            m_provider = str(manifest.get("provider") or "").strip()
            if m_provider != "github-raw":
                problems.append("provider='%s' (github-raw만 지원)" % (m_provider or "없음"))
            if not str(manifest.get("version_file") or "").strip():
                problems.append("version_file 없음")
            # 가이드 272행: version_key는 "권장" — 누락 시 경고로 처리
            m_version_key_missing = not str(manifest.get("version_key") or "").strip()
            missing_files = [rel for rel in manifest_files
                             if not os.path.isfile(os.path.join(plugin_dir, rel))]
            if missing_files:
                problems.append("files에 선언된 파일이 없음: " + ", ".join(missing_files[:3]))
            if not str(manifest.get("raw_base_url") or "").strip():
                problems.append("raw_base_url이 비어 있음")
            if problems:
                checks.append({"name": "update_manifest", "ok": False,
                               "detail": "; ".join(problems[:4]),
                               "guide_ref": "가이드 §3.1 플러그인 내부 업데이트 계약 (update_manifest 규격)"})
            elif m_version_key_missing:
                checks.append({"name": "update_manifest", "ok": True, "warn": True,
                               "detail": "provider/files %d개/raw_base_url/version_file 확인 — version_key 미선언 (권장)" % len(manifest_files),
                               "guide_ref": "가이드 §3.1 플러그인 내부 업데이트 계약 (update_manifest 규격)"})
            else:
                checks.append({"name": "update_manifest", "ok": True,
                               "detail": "provider/files %d개/raw_base_url/version_file/version_key 확인" % len(manifest_files),
                               "guide_ref": "가이드 §3.1 플러그인 내부 업데이트 계약 (update_manifest 규격)"})
        else:
            checks.append({"name": "update_manifest", "ok": True,
                           "detail": "미선언 (업데이트 미지원)",
                           "guide_ref": "가이드 §3.1 플러그인 내부 업데이트 계약 (update_manifest 규격)"})

        # 9. UI 번들 검사 (category_tab 선언 시 index.html/script.js/style.css 필수)
        ui_files = {f: os.path.isfile(os.path.join(plugin_dir, f))
                    for f in ("index.html", "script.js", "style.css")}
        if "category_tab" in cls_attrs:
            missing_ui = [f for f, ok in ui_files.items() if not ok]
            if missing_ui:
                checks.append({"name": "UI 번들", "ok": False,
                               "detail": "category_tab 선언 시 필수: " + ", ".join(missing_ui),
                               "guide_ref": "가이드 §2 디렉토리 구조 (index.html/script.js/style.css)"})
            else:
                checks.append({"name": "UI 번들", "ok": True, "detail": "index/script/style 확인",
                               "guide_ref": "가이드 §2 디렉토리 구조 (index.html/script.js/style.css)"})
        else:
            present_ui = [f for f, ok in ui_files.items() if ok]
            if present_ui and len(present_ui) < 3:
                checks.append({"name": "UI 번들", "ok": True, "warn": True,
                               "detail": "경고: UI 파일 일부만 존재 (" + ", ".join(present_ui) + ")",
                               "guide_ref": "가이드 §2 디렉토리 구조 (index.html/script.js/style.css)"})
            elif present_ui:
                checks.append({"name": "UI 번들", "ok": True, "detail": "UI 번들 확인",
                               "guide_ref": "가이드 §2 디렉토리 구조 (index.html/script.js/style.css)"})
            else:
                checks.append({"name": "UI 번들", "ok": True, "detail": "미선언",
                               "guide_ref": "가이드 §2 디렉토리 구조 (index.html/script.js/style.css)"})

        # 10. requirements.txt 격리 규정 검사 (가이드 2장 60~61행: libs/ 격리 + 코어 라이브러리 보호)
        req_path = os.path.join(plugin_dir, "requirements.txt")
        req_danger = []
        req_lines = []
        if os.path.isfile(req_path):
            try:
                with open(req_path, "r", encoding="utf-8", errors="replace") as f:
                    req_lines = [ln.strip() for ln in f
                                 if ln.strip() and not ln.strip().startswith("#")]
                core_protected = ("flask", "pymupdf", "fitz", "pillow", "pil",
                                  "sqlalchemy", "werkzeug", "jinja2")
                for ln in req_lines:
                    # 패키지명 추출 (==, >=, <=, ~=, !=, 공백 구분자)
                    pkg = ln.split("=")[0].split(">")[0].split("<")[0].split("~")[0].split("!")[0].strip()
                    pkg_l = pkg.lower().replace("_", "-").replace(".", "-")
                    if pkg_l in core_protected or pkg_l.split("-")[0] in core_protected:
                        req_danger.append(pkg or ln)
            except Exception:
                req_danger = []
            if req_danger:
                checks.append({"name": "requirements.txt 격리", "ok": False,
                               "detail": "코어 보호 패키지 덮어쓰기 위험: "
                                         + ", ".join(sorted(set(req_danger))[:4]),
                               "guide_ref": "가이드 §2.1 보안 제약 (패키지 격리 — 코어 라이브러리 보호)"})
            else:
                checks.append({"name": "requirements.txt 격리", "ok": True,
                               "detail": "코어 보호 패키지 충돌 없음 (%d개 패키지)" % len(req_lines),
                               "guide_ref": "가이드 §2.1 보안 제약 (패키지 격리 — 코어 라이브러리 보호)"})
        else:
            checks.append({"name": "requirements.txt 격리", "ok": True,
                           "detail": "미존재 (격리 대상 없음)",
                           "guide_ref": "가이드 §2.1 보안 제약 (패키지 격리 — 코어 라이브러리 보호)"})

        all_ok = all(c.get("ok") for c in checks)
        return all_ok, checks

    def _update_plugin(self, plugin_id, db_type):
        """특정 플러그인 업데이트 실행 (릴리즈 태그 우선, 브랜치 폴백 — 코어 sample_update_plugin 미사용)"""
        pdir, err = self._validate_plugin_path(plugin_id)
        if err or not pdir:
            return False, err or "유효하지 않은 플러그인 ID입니다."

        if not os.path.exists(pdir):
            return False, f"플러그인을 찾을 수 없습니다: {plugin_id}"

        try:
            from services.metadata_factory import MetadataFactory
            _, target_cls = MetadataFactory._import_provider_module_and_class(plugin_id)
        except Exception as e:
            return False, f"플러그인 로드 실패: {e}"

        manifest = getattr(target_cls, "update_manifest", None)
        spec = self._build_update_spec(plugin_id, manifest)
        if not spec:
            return False, "update_manifest 가 없거나 유효하지 않아 업데이트할 수 없습니다."

        local_ver = self._read_local_plugin_version(pdir, spec["version_file"], spec["version_key"])
        base_url = self._resolve_update_base_url(
            plugin_id, spec["raw_base_url"], spec.get("files"), db_type
        )
        remote_ver = self._fetch_remote_plugin_version(
            base_url,
            version_file=spec["version_file"],
            version_key=spec["version_key"],
        )

        if not remote_ver:
            return False, f"원격 버전을 확인할 수 없습니다. (소스: {base_url})"
        if not self._can_update_to_version(local_ver, remote_ver):
            return False, (
                f"업데이트 불가: 원격 버전({remote_ver})이 현재 버전({local_ver or '알 수 없음'})보다 "
                f"낮거나 같습니다."
            )

        # 파일 다운로드 → 교체
        # Gitea 소스면 해당 호스트 토큰 사용 (비공개 저장소 인증)
        gitea_token = None
        raw_parsed = self._parse_raw_base_url(spec["raw_base_url"])
        if raw_parsed:
            src_host = re.sub(r"^https?://", "", str(spec["raw_base_url"])).split("/")[0]
            gitea_token = self._gitea_token_for_host(db_type, src_host)
        downloaded = {}
        for name in spec["files"]:
            file_url = f"{base_url.rstrip('/')}/{name}"
            try:
                downloaded[name] = self._fetch_text(file_url, token=gitea_token)
            except HTTPError as e:
                if e.code == 404:
                    return False, f"원격 저장소에서 파일을 찾을 수 없습니다: {name}"
                raise
            except Exception as e:
                return False, f"파일 다운로드 실패 ({name}): {str(e)}"

        for name, content in downloaded.items():
            fpath = os.path.join(pdir, name)
            fdir = os.path.dirname(fpath)
            if fdir and not os.path.exists(fdir):
                os.makedirs(fdir, exist_ok=True)
            with open(fpath, "w", encoding="utf-8", newline="") as f:
                f.write(content)

        # 핫 리로드 (업데이트 자체는 성공 유지, 리로드 실패는 경고로 포함)
        reload_warning = ""
        try:
            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)
        except Exception as e:
            reload_warning = f" (단, 리로드 실패: {str(e)})"

        source_label = "릴리즈 태그" if base_url != spec["raw_base_url"] else "브랜치(main)"
        return True, f"'{plugin_id}' 플러그인이 업데이트되었습니다 (v{remote_ver}, {source_label} 기준).{reload_warning}"

    def _update_all_plugins(self, db_type):
        """설치된 모든 플러그인 일괄 업데이트"""
        plugins = self._list_plugins(db_type)
        updated_count = 0
        failed = []

        for p in plugins:
            pid = p['id']
            if pid == 'plugin_manager':
                continue
            if p.get('has_update_manifest'):
                ok, msg = self._update_plugin(pid, db_type)
                if ok:
                    updated_count += 1
                else:
                    failed.append(f"{pid}: {msg}")

        res_msg = f"총 {updated_count}개 플러그인이 성공적으로 업데이트되었습니다."
        if failed:
            res_msg += " (실패: " + ", ".join(failed) + ")"
        return True, res_msg

    def _validate_plugin_path(self, plugin_id):
        """플러그인 ID 및 경로 안전성 검증 (플러그인 폴더 경계 이탈 차단)"""
        pid = str(plugin_id or "").strip()
        if not pid or not re.match(r'^[a-zA-Z0-9_-]+$', pid):
            return None, "유효하지 않은 플러그인 ID입니다 (영문, 숫자, 언더바, 하이픈만 허용)."

        base_dir = os.path.realpath(self._get_plugins_base_dir())
        target_path = os.path.realpath(os.path.join(base_dir, pid))

        if not target_path.startswith(base_dir + os.sep):
            return None, "플러그인 폴더(plugins/metadata) 경계를 벗어난 접근은 엄격히 금지됩니다."

        return target_path, None

    def _delete_plugin(self, plugin_id, db_type):
        """플러그인 삭제 (플러그인 폴더 경계 이탈 엄격 차단)"""
        if plugin_id in ("plugin_manager", "base.py"):
            return False, "시스템 핵심 플러그인 매니저는 삭제할 수 없습니다."

        pdir, err = self._validate_plugin_path(plugin_id)
        if err or not pdir:
            return False, err or "유효하지 않은 플러그인 삭제 요청입니다."

        if not os.path.exists(pdir):
            return False, f"존재하지 않는 플러그인입니다: {plugin_id}"

        try:
            shutil.rmtree(pdir)
            self._sources_delete(plugin_id)  # 소스 메타(sqlite)도 함께 정리
            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)
            return True, f"플러그인 '{plugin_id}'가 성공적으로 삭제되었습니다."
        except Exception as e:
            return False, f"플러그인 삭제 실패: {str(e)}"

    def _toggle_plugin(self, plugin_id, enabled_val, db_type):
        """플러그인 활성화/비활성화 토글

        가이드 부합: PluginService 직접 import 대신 get_db_gateway 헬퍼로
        general 세션 DB의 PLUGIN_ENABLED_<id> 설정을 직접 쓴다.
        """
        try:
            self.get_db_gateway('general').set_setting(
                f"PLUGIN_ENABLED_{plugin_id}", str(enabled_val)
            )

            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)

            status_text = "활성화" if str(enabled_val) == "1" else "비활성화"
            return True, f"플러그인 '{plugin_id}' 상태가 '{status_text}'로 변경되었습니다."
        except Exception as e:
            return False, f"플러그인 토글 중 오류 발생: {str(e)}"

    # ------------------------------------------------------------------
    # Plugin Source Meta (sqlite plugin_sources.db)
    # .git_source/.zip_source 파일 대신 소스 메타를 DB에 저장.
    # 설치 후 최초 1회 레거시 파일 → DB 마이그레이션 수행.
    # ------------------------------------------------------------------

    def _get_sources_db_path(self):
        """소스 메타 SQLite DB 경로 (플러그인 폴더 내 plugin_sources.db — git 저장소에 미포함)"""
        return os.path.join(self._get_plugins_base_dir(), "plugin_manager", "plugin_sources.db")

    def _sources_init_db(self):
        """plugin_sources.db 스키마 보장 (WAL). 호출마다 CREATE IF NOT EXISTS — 접근은 _SOURCES_DB_LOCK"""
        db_path = self._get_sources_db_path()
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        except Exception:
            pass
        with _SOURCES_DB_LOCK:
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS plugin_sources (
                        plugin_id      TEXT PRIMARY KEY,
                        git_url        TEXT,
                        branch         TEXT,
                        manifest_files TEXT,
                        installed_at   TEXT
                    )
                    """
                )
                # 구버전 스키마(source_type/filename 컬럼)에서 전환 — git_url 유무가
                # 소스 메타 판단 기준이므로 불필요한 컬럼 제거 (sqlite 3.35+ DROP COLUMN)
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(plugin_sources)")}
                    for drop_col in ("source_type", "filename"):
                        if drop_col in cols:
                            conn.execute(f"ALTER TABLE plugin_sources DROP COLUMN {drop_col}")
                except Exception:
                    pass
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _sources_db_query(self, sql, params=()):
        """plugin_sources.db SELECT (락 내부, dict 리스트 반환)"""
        self._sources_init_db()
        with _SOURCES_DB_LOCK:
            conn = sqlite3.connect(self._get_sources_db_path(), timeout=10)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def _sources_db_execute(self, sql, params=()):
        """plugin_sources.db write (락 내부, commit)"""
        self._sources_init_db()
        with _SOURCES_DB_LOCK:
            conn = sqlite3.connect(self._get_sources_db_path(), timeout=10)
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()

    def _sources_get(self, plugin_id):
        """plugin_sources 레코드 → dict (git_url 없으면 None — 로컬 플러그인과 동치)"""
        try:
            rows = self._sources_db_query(
                "SELECT git_url, branch, manifest_files, installed_at "
                "FROM plugin_sources WHERE plugin_id = ?",
                (str(plugin_id),),
            )
            if not rows:
                return None
            r = rows[0]
            if not (r.get("git_url") or "").strip():
                return None
            info = {"git_url": r["git_url"]}
            if r.get("branch"):
                info["branch"] = r["branch"]
            if r.get("manifest_files"):
                try:
                    info["manifest_files"] = json.loads(r["manifest_files"])
                except Exception:
                    info["manifest_files"] = []
            if r.get("installed_at"):
                info["installed_at"] = r["installed_at"]
            return info
        except Exception:
            return None

    def _sources_set(self, plugin_id, info):
        """plugin_sources upsert — git_url이 있어야 저장.
        설치 방식과 무관하게 git_url 유무가 소스 메타 판단 기준이므로,
        git_url 없는 입력(zip 업로드 등)은 저장하지 않는다."""
        if not isinstance(info, dict):
            return
        git_url = str(info.get("git_url") or "").strip()
        if not git_url:
            return
        try:
            mf = info.get("manifest_files")
            mf_json = json.dumps(mf, ensure_ascii=False) if mf else None
            self._sources_db_execute(
                "INSERT OR REPLACE INTO plugin_sources "
                "(plugin_id, git_url, branch, manifest_files, installed_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    str(plugin_id),
                    git_url,
                    str(info.get("branch") or "") or None,
                    mf_json,
                    str(info.get("installed_at") or "") or None,
                ),
            )
        except Exception:
            pass

    def _sources_delete(self, plugin_id):
        """plugin_sources 레코드 삭제 (플러그인 삭제 시 호출)"""
        try:
            self._sources_db_execute(
                "DELETE FROM plugin_sources WHERE plugin_id = ?", (str(plugin_id),)
            )
        except Exception:
            pass

    def _sources_meta_get(self, key):
        try:
            rows = self._sources_db_query("SELECT value FROM meta WHERE key = ?", (key,))
            return rows[0]["value"] if rows else None
        except Exception:
            return None

    def _sources_meta_set(self, key, value):
        try:
            self._sources_db_execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value))
            )
        except Exception:
            pass

    def _sources_migrate_legacy_files(self):
        """설치 후 최초 1회: 레거시 .git_source/.zip_source 파일을 DB로 이동 후 파일 삭제,
        소스 메타가 없는 수동 설치본은 update_manifest의 raw_base_url로 GitHub 소스를 추론해 백필.

        1) .git_source/.zip_source 파일 → plugin_sources upsert + 파일 삭제
        2) 소스 메타가 아직 없는 플러그인 + update_manifest(raw_base_url) 선언 → git_url 메타 백필
           (monorepo 서브디렉토리는 릴리즈 태그 기준이 달라 제외 — _ensure_git_source_from_raw_base_url 참고)
        완료 여부는 DB meta(legacy_migration_done)에 기록 — DB가 없어도 재시도 가능.
        실패해도 조용히 스킵 (다음 호출 시 재시도)."""
        global _SOURCES_MIGRATION_DONE
        if _SOURCES_MIGRATION_DONE:
            return
        with _SOURCES_MIGRATION_LOCK:
            if _SOURCES_MIGRATION_DONE:
                return
            try:
                if self._sources_meta_get("legacy_migration_done") == "1":
                    _SOURCES_MIGRATION_DONE = True
                    return
            except Exception:
                pass
            try:
                base_dir = self._get_plugins_base_dir()
                for entry in sorted(os.listdir(base_dir)):
                    pdir = os.path.join(base_dir, entry)
                    if not os.path.isdir(pdir):
                        continue
                    for meta_file in (".git_source", ".zip_source"):
                        path = os.path.join(pdir, meta_file)
                        if not os.path.isfile(path):
                            continue
                        try:
                            with open(path, "r", encoding="utf-8") as f:
                                info = json.load(f)
                            # git_url 보유 파일만 DB로 이동 (source_type 무관 — git_url 유무가 판단 기준).
                            # .zip_source는 git_url이 없어 저장되지 않고, 아래 백필에서
                            # update_manifest 기준으로 재판단된다.
                            if isinstance(info, dict) and str(info.get("git_url") or "").strip():
                                self._sources_set(entry, info)
                        except Exception:
                            pass
                        finally:
                            # 파싱 실패(손상 파일)여도 DB 기반 전환 후 파일은 제거
                            try:
                                os.remove(path)
                            except Exception:
                                pass
                    # 2) update_manifest 기반 백필 — 소스 메타가 없는 수동 설치본
                    if self._sources_get(entry) is None:
                        try:
                            files_clean, manifest = self._extract_update_manifest_files(pdir)
                            raw_base_url = str((manifest or {}).get("raw_base_url") or "").strip().rstrip("/")
                            if files_clean and raw_base_url:
                                self._ensure_git_source_from_raw_base_url(entry, raw_base_url, files_clean)
                        except Exception:
                            pass
                self._sources_meta_set("legacy_migration_done", "1")
                _SOURCES_MIGRATION_DONE = True
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Plugin Catalog (GitHub 토픽 기반 자동 수집 — 미설치 플러그인 발견)
    # 설정(간격/토픽)은 코어 DB(gateway → MariaDB), 조회 결과만 catalog.db(sqlite)
    # ------------------------------------------------------------------

    def _get_catalog_db_path(self):
        """카탈로그 SQLite DB 경로 (플러그인 폴더 내 catalog.db)"""
        return os.path.join(self._get_plugins_base_dir(), "plugin_manager", "catalog.db")

    def _catalog_full_name_from_url(self, git_url):
        """git_url → full_name (owner/repo). GitHub가 아니면 None."""
        try:
            repo = self._parse_github_repo(git_url)
            if repo:
                return "{}/{}".format(repo[0], repo[1])
        except Exception:
            pass
        return None

    def _catalog_record_install_error(self, git_url, message):
        """Git 설치 실패 메시지를 catalog.db repos.install_error에 저장 (최대 2000자)."""
        full_name = self._catalog_full_name_from_url(git_url)
        if not full_name:
            return
        try:
            self._catalog_init_db()
            err = str(message or "")[:2000]
            self._catalog_db_execute(
                "UPDATE repos SET install_error=? WHERE full_name=?", (err, full_name)
            )
        except Exception:
            pass

    def _catalog_clear_install_error(self, git_url):
        """Git 설치 성공 시 install_error 초기화."""
        full_name = self._catalog_full_name_from_url(git_url)
        if not full_name:
            return
        try:
            self._catalog_init_db()
            self._catalog_db_execute(
                "UPDATE repos SET install_error=NULL WHERE full_name=?", (full_name,)
            )
        except Exception:
            pass

    def _catalog_init_db(self):
        """catalog.db 스키마 보장 (WAL). 호출마다 CREATE IF NOT EXISTS — 접근은 _CATALOG_DB_LOCK"""
        db_path = self._get_catalog_db_path()
        try:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        except Exception:
            pass
        with _CATALOG_DB_LOCK:
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS repos (
                        full_name      TEXT PRIMARY KEY,
                        html_url       TEXT,
                        description    TEXT,
                        topics         TEXT,
                        default_branch TEXT,
                        pushed_at      TEXT,
                        plugin_id      TEXT,
                        plugin_name    TEXT,
                        latest_version TEXT,
                        is_valid       TEXT DEFAULT 'unknown',
                        last_checked   TEXT,
                        install_error  TEXT,
                        source         TEXT DEFAULT 'github',
                        base_url       TEXT
                    )
                    """
                )
                # 기존 DB(구버전 스키마)에 누락 컬럼 추가
                repo_cols = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
                if "plugin_name" not in repo_cols:
                    conn.execute("ALTER TABLE repos ADD COLUMN plugin_name TEXT")
                if "install_error" not in repo_cols:
                    conn.execute("ALTER TABLE repos ADD COLUMN install_error TEXT")
                if "source" not in repo_cols:
                    conn.execute("ALTER TABLE repos ADD COLUMN source TEXT DEFAULT 'github'")
                if "base_url" not in repo_cols:
                    conn.execute("ALTER TABLE repos ADD COLUMN base_url TEXT")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key   TEXT PRIMARY KEY,
                        value TEXT
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _catalog_db_query(self, sql, params=()):
        """catalog.db SELECT (락 내부, dict 리스트 반환)"""
        with _CATALOG_DB_LOCK:
            conn = sqlite3.connect(self._get_catalog_db_path(), timeout=10)
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def _catalog_db_execute(self, sql, params=()):
        """catalog.db write (락 내부, commit)"""
        with _CATALOG_DB_LOCK:
            conn = sqlite3.connect(self._get_catalog_db_path(), timeout=10)
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()

    def _catalog_read_meta(self):
        """meta 테이블 전체 dict (없으면 빈 dict)"""
        try:
            rows = self._catalog_db_query("SELECT key, value FROM meta")
            return {r["key"]: r["value"] for r in rows}
        except Exception:
            return {}

    def _catalog_set_meta(self, key, value):
        """meta 테이블 key/value upsert"""
        self._catalog_db_execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value))
        )

    # ---- 설정 (코어 DB — gateway → MariaDB) ----

    def _catalog_clamp_interval(self, raw):
        """갱신 간격 1~24 클램프 (파싱 불가/빈 값이면 기본 6)"""
        try:
            val = int(str(raw or "").strip())
        except Exception:
            return _CATALOG_DEFAULT_INTERVAL_HOURS
        if val < _CATALOG_MIN_INTERVAL_HOURS:
            return _CATALOG_MIN_INTERVAL_HOURS
        if val > _CATALOG_MAX_INTERVAL_HOURS:
            return _CATALOG_MAX_INTERVAL_HOURS
        return val

    def _catalog_get_interval_hours(self, db_type):
        """PM_CATALOG_REFRESH_HOURS 조회 (클램프 재적용 — 외부 직접 수정 대비)"""
        try:
            gateway = self.get_db_gateway(db_type)
            raw = gateway.get_setting("PM_CATALOG_REFRESH_HOURS", default=None)
            if isinstance(raw, dict):
                raw = raw.get("value")
            return self._catalog_clamp_interval(raw)
        except Exception:
            return _CATALOG_DEFAULT_INTERVAL_HOURS

    def _catalog_normalize_topics(self, raw):
        """
        토픽 목록 정규화: 쉼표/줄바꿈/공백 분리 → 형식 검증(소문자 영숫자+하이픈) →
        중복 제거 → 최대 _CATALOG_MAX_TOPICS개 → 빈 목록이면 기본값 복원
        """
        if raw is None:
            return list(_CATALOG_DEFAULT_TOPICS)
        if isinstance(raw, (list, tuple)):
            parts = [str(t) for t in raw]
        else:
            parts = re.split(r"[\s,]+", str(raw))
        seen = []
        for part in parts:
            topic = part.strip().lower()
            if not topic:
                continue
            if not _CATALOG_TOPIC_RE.match(topic):
                continue
            if topic in seen:
                continue
            seen.append(topic)
        if not seen:
            return list(_CATALOG_DEFAULT_TOPICS)
        return seen[:_CATALOG_MAX_TOPICS]

    def _catalog_get_topics(self, db_type):
        """PM_CATALOG_TOPICS 조회 (쉼표 구분 문자열 → 정규화된 리스트)"""
        try:
            gateway = self.get_db_gateway(db_type)
            raw = gateway.get_setting("PM_CATALOG_TOPICS", default=None)
            if isinstance(raw, dict):
                raw = raw.get("value")
            return self._catalog_normalize_topics(raw)
        except Exception:
            return list(_CATALOG_DEFAULT_TOPICS)

    # ---- Gitea 서버 설정 ----

    def _catalog_get_gitea_servers(self, db_type):
        """PM_CATALOG_GITEA_SERVERS 조회 → [{url, token, host}] 목록 (정규화).
        url은 https:// 강제, 중복 host 제거, 토큰은 빈 문자열 가능.
        설정 없으면 빈 목록 (Gitea 비활성)."""
        try:
            gateway = self.get_db_gateway(db_type)
            raw = gateway.get_setting("PM_CATALOG_GITEA_SERVERS", default=None)
            if isinstance(raw, dict):
                raw = raw.get("value")
            if not raw:
                return []
            data = json.loads(str(raw)) if isinstance(raw, str) else raw
            if not isinstance(data, list):
                return []
            servers = []
            seen_hosts = set()
            for item in data:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip().rstrip("/")
                if not url.startswith("https://"):
                    continue
                host = re.sub(r"^https?://", "", url).split("/")[0]
                if not host or host in seen_hosts:
                    continue
                seen_hosts.add(host)
                token = str(item.get("token") or "").strip()
                servers.append({"url": url, "host": host, "token": token})
            return servers
        except Exception:
            return []

    def _gitea_server_for_host(self, db_type, host):
        """호스트에 해당하는 Gitea 서버 설정 dict 반환 (없으면 None)"""
        host = str(host or "").strip().lower()
        for s in self._catalog_get_gitea_servers(db_type):
            if str(s.get("host") or "").lower() == host:
                return s
        return None

    def _gitea_token_for_host(self, db_type, host):
        """Gitea 호스트의 토큰 반환 (미등록/미설정이면 None)"""
        s = self._gitea_server_for_host(db_type, host)
        return (s or {}).get("token") or None

    def _catalog_validate_gitea_servers(self, raw):
        """저장 전 Gitea 서버 목록 검증 → 정규화된 JSON 문자열 (무효 항목 제거).
        - url: https:// 강제
        - host 중복 제거
        - 토큰은 마스킹 형태(앞 4 + ***)면 무시 (프론트가 내려준 실제 값만 저장)"""
        servers = []
        seen_hosts = set()
        if isinstance(raw, (list, tuple)):
            items = raw
        else:
            try:
                items = json.loads(str(raw or "[]"))
            except Exception:
                items = []
            if not isinstance(items, list):
                items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip().rstrip("/")
            if not url.startswith("https://"):
                continue
            host = re.sub(r"^https?://", "", url).split("/")[0]
            if not host or host in seen_hosts:
                continue
            seen_hosts.add(host)
            token = str(item.get("token") or "").strip()
            # 프론트에서 보낸 마스킹 표시(등록된 토큰 유지 요청)면 실제 값을
            # 유지하기 위해 별도 처리 — 여기서는 저장하지 않고 호출부에서 처리.
            servers.append({"url": url, "token": token})
        return json.dumps(servers, ensure_ascii=False)

    # ---- GitHub 조회 ----

    def _catalog_get_github_token(self, db_type):
        """PM_GITHUB_TOKEN 조회 (MariaDB 설정). 설정돼 있으면 Bearer 헤더로 사용.
        토큰 사용 시 GitHub Search API 제한이 IP 기준이 아닌 계정 기준 5,000/hr가 되어
        공용/클라우드 IP 제한(403)에서 벗어난다."""
        try:
            gateway = self.get_db_gateway(db_type)
            raw = gateway.get_setting("PM_GITHUB_TOKEN", default=None)
            if isinstance(raw, dict):
                raw = raw.get("value")
            tok = str(raw or "").strip()
            return tok or None
        except Exception:
            return None

    def _catalog_search_topic(self, db_type, topic, source="github", gitea_server=None):
        """토픽 검색 (per_page=100). 응답 dict 반환 (예외 전파).

        - GitHub: Search API (토큰 설정 시 Authorization: Bearer ***)
        - Gitea : 등록 서버의 /api/v1/repos/search?topic=... (토큰 필요 시 token ***)
        """
        if source == "gitea":
            if not gitea_server:
                raise RuntimeError("Gitea 서버 설정이 없습니다.")
            url = "{0}/api/v1/repos/search?topic={1}&limit=50&page=1".format(
                gitea_server["url"], topic
            )
            headers = {"User-Agent": "BookOasis/1.0", "Accept": "application/json"}
            token = gitea_server.get("token") or ""
            if token:
                headers["Authorization"] = "token {0}".format(token)
            req = Request(url, headers=headers)
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        url = "https://api.github.com/search/repositories?q=topic:{0}&per_page=100".format(topic)
        headers = {
            "User-Agent": "BookOasis/1.0",
            "Accept": "application/vnd.github+json",
        }
        token = self._catalog_get_github_token(db_type)
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(url, headers=headers)
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _catalog_search_gitea_topic(self, db_type, topic, server):
        """Gitea 토픽 검색 → GitHub과 동일한 {items:[...]} 형태로 정규화.
        Gitea API 응답: {data: [ {full_name, html_url, description, topics, default_branch, updated_at, private}, ... ]}
        """
        raw = self._catalog_search_topic(db_type, topic, source="gitea", gitea_server=server)
        items = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("Gitea Search API 응답 오류 (topic={0}, server={1})".format(
                topic, server.get("host", "?")
            ))
        out = []
        wanted = set()
        for part in str(topic).lower().replace(",", " ").split():
            if part:
                wanted.add(part)
        for it in items:
            if not isinstance(it, dict):
                continue
            full_name = str(it.get("full_name") or "").strip()
            if not full_name:
                continue
            # Gitea Search API는 topic 파라미터를 완전일치 필터로 보장하지 않음 → 클라이언트에서 필터
            if wanted:
                it_topics = {str(t).lower() for t in (it.get("topics") or [])}
                if not it_topics.intersection(wanted):
                    continue
            out.append({
                "full_name": full_name,
                "html_url": str(it.get("html_url") or ""),
                "description": str(it.get("description") or "")[:500],
                "topics": json.dumps(it.get("topics") or [], ensure_ascii=False),
                "default_branch": str(it.get("default_branch") or "main"),
                "pushed_at": str(it.get("updated_at") or ""),
                "_source": "gitea",
                "_base_url": server["url"],
            })
        return {"items": out}

    def _catalog_check_repo_version(self, full_name, default_branch, source="github", base_url=None, db_type=None):
        """
        raw VERSION 조회 → (is_valid, plugin_id, latest_version, plugin_name).
        GitHub: raw.githubusercontent.com, Gitea: {base}/raw/branch/{branch}/VERSION (토큰 필요 시 사용).
        name은 VERSION JSON 키 → 없으면 코드 raw fetch 후 AST(name 클래스 속성) 추출.
        404/비JSON/네트워크 오류 → invalid (다음 주기 재판정).
        """
        branch = str(default_branch or "main").strip() or "main"
        if source == "gitea" and base_url:
            url = "{0}/{1}/raw/branch/{2}/VERSION".format(base_url, full_name, branch)
        else:
            url = "https://raw.githubusercontent.com/{0}/{1}/VERSION".format(full_name, branch)
        try:
            token = None
            if source == "gitea" and base_url:
                token = self._gitea_token_for_host(db_type, self._host_of_url(base_url))
            text = self._fetch_text(url, timeout=15, token=token)
            version, plugin_id, name = self._catalog_parse_remote_version_meta(text)
            if not version:
                return "invalid", None, None, None
            if not plugin_id:
                plugin_id = str(full_name).split("/")[-1]
            if not name:
                name = self._catalog_fetch_plugin_name(full_name, branch, plugin_id, source, base_url, db_type)
            return "valid", plugin_id, version, name
        except Exception:
            return "invalid", None, None, None

    def _host_of_url(self, url):
        """URL에서 호스트 추출 (예: https://gitea.example.com → gitea.example.com)"""
        try:
            return re.sub(r"^https?://", "", str(url)).split("/")[0]
        except Exception:
            return ""

    def _catalog_parse_remote_version_meta(self, text):
        """VERSION 텍스트 → (version, plugin_id, name). JSON dict의 id/plugin_id/name 키 최우선."""
        version, plugin_id, name = None, None, None
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("plugin version", "plugin_version", "version"):
                    if data.get(key):
                        version = str(data[key]).strip()
                        break
                pid = data.get("id") or data.get("plugin_id")
                if pid:
                    plugin_id = str(pid).strip()
                pname = data.get("name") or data.get("plugin_name")
                if pname:
                    name = str(pname).strip()
        except Exception:
            pass
        if not version:
            version = self._parse_remote_version(text)
        return version, plugin_id, name

    def _catalog_fetch_plugin_name(self, full_name, branch, plugin_id, source="github", base_url=None, db_type=None):
        """코드 raw 파일에서 BaseMetadataProvider name 클래스 속성 추출 (없으면 None).
        GitHub: raw.githubusercontent.com, Gitea: {base}/raw/branch/{branch}"""
        try:
            if source == "gitea" and base_url:
                src_url = "{0}/{1}/raw/branch/{2}/{3}.py".format(
                    base_url, full_name, branch, plugin_id
                )
                token = self._gitea_token_for_host(db_type, self._host_of_url(base_url))
            else:
                src_url = "https://raw.githubusercontent.com/{0}/{1}/{2}.py".format(
                    full_name, branch, plugin_id
                )
                token = None
            source_text = self._fetch_text(src_url, timeout=15, token=token)
        except Exception:
            return None
        if not source_text:
            return None
        try:
            tree = ast.parse(source_text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if not isinstance(stmt, ast.Assign):
                        continue
                    if not any(isinstance(t, ast.Name) and t.id == "name" for t in stmt.targets):
                        continue
                    try:
                        val = ast.literal_eval(stmt.value)
                    except Exception:
                        continue
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except Exception:
            pass
        return None

    # ---- 갱신 로직 ----

    def _catalog_refresh_once(self, db_type):
        """
        카탈로그 1회 갱신: 토픽별 Search API → repos upsert → VERSION 판별.
        중복 실행 방지: meta refresh_state=running 이면 즉시 return.
        실패 시 refresh_state=error 기록 후 예외 전파 (스레드 루프가 다음 주기 재시도).
        """
        self._catalog_init_db()
        meta = self._catalog_read_meta()
        if meta.get("refresh_state") == "running":
            started_raw = meta.get("refresh_started_at") or ""
            is_stale = False
            if started_raw:
                try:
                    started_dt = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - started_dt).total_seconds() > 300:
                        is_stale = True
                except Exception:
                    is_stale = True
            else:
                is_stale = True

            if not is_stale:
                return

        now_str = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        self._catalog_set_meta("refresh_state", "running")
        self._catalog_set_meta("refresh_started_at", now_str)
        self._catalog_set_meta("refresh_error", "")

        try:
            topics = self._catalog_get_topics(db_type)
            if not topics:
                topics = list(_CATALOG_DEFAULT_TOPICS)

            # 1. 토픽별 Search API (GitHub + 등록된 모든 Gitea 서버) → (source, full_name) 기준 합집합
            merged = {}
            for topic in topics:
                data = self._catalog_search_topic(db_type, topic)
                if not isinstance(data, dict) or "items" not in data:
                    raise RuntimeError(
                        "GitHub Search API 응답 오류 (rate limit 또는 일시 오류일 수 있음): topic={0}".format(topic)
                    )
                for it in data.get("items", []):
                    full_name = str(it.get("full_name") or "").strip()
                    if not full_name:
                        continue
                    merged[("github", full_name)] = {
                        "html_url": str(it.get("html_url") or ""),
                        "description": str(it.get("description") or "")[:500],
                        "topics": json.dumps(it.get("topics") or [], ensure_ascii=False),
                        "default_branch": str(it.get("default_branch") or "main"),
                        "pushed_at": str(it.get("pushed_at") or ""),
                        "source": "github",
                        "base_url": "https://github.com",
                    }

            gitea_servers = self._catalog_get_gitea_servers(db_type)
            gitea_errors = []
            for server in gitea_servers:
                try:
                    for topic in topics:
                        data = self._catalog_search_gitea_topic(db_type, topic, server)
                        for it in data.get("items", []):
                            full_name = str(it.get("full_name") or "").strip()
                            if not full_name:
                                continue
                            merged[("gitea", full_name)] = {
                                "html_url": str(it.get("html_url") or ""),
                                "description": str(it.get("description") or "")[:500],
                                "topics": json.dumps(it.get("topics") or [], ensure_ascii=False),
                                "default_branch": str(it.get("default_branch") or "main"),
                                "pushed_at": str(it.get("pushed_at") or ""),
                                "source": "gitea",
                                "base_url": server["url"],
                            }
                except Exception as e:
                    # 개별 Gitea 서버 실패는 전체 갱신을 막지 않음 (GitHub는 이미 수집됨)
                    gitea_errors.append("{0}: {1}".format(server.get("host", "?"), str(e)[:200]))

            # 2. repos upsert (검색 메타만 — is_valid/last_checked는 보존)
            for (source, full_name), info in merged.items():
                self._catalog_db_execute(
                    """
                    INSERT INTO repos
                        (full_name, html_url, description, topics, default_branch, pushed_at, plugin_id, latest_version, is_valid, last_checked, source, base_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'unknown', NULL, ?, ?)
                    ON CONFLICT(full_name) DO UPDATE SET
                        html_url=excluded.html_url,
                        description=excluded.description,
                        topics=excluded.topics,
                        default_branch=excluded.default_branch,
                        pushed_at=excluded.pushed_at,
                        plugin_id=excluded.plugin_id,
                        source=excluded.source,
                        base_url=excluded.base_url,
                        install_error=NULL
                    """,
                    (
                        full_name,
                        info["html_url"],
                        info["description"],
                        info["topics"],
                        info["default_branch"],
                        info["pushed_at"],
                        str(full_name).split("/")[-1],
                        source,
                        info["base_url"],
                    ),
                )

            # 3. 검색에서 더 이상 조회되지 않는 미설치 저장소 정리 (DB 제거)
            #    설치된 플러그인은 검색과 무관하게 유지 (업데이트/정보 보존).
            #    모든 토픽 검색이 성공한 시점에만 실행 — GitHub 실패 시 위에서 raise 되어 도달 안 함.
            removed = 0
            base_dir = self._get_plugins_base_dir()
            for r in self._catalog_db_query("SELECT full_name, plugin_id FROM repos"):
                key = (str(r.get("source") or "github"), r["full_name"])
                if key in merged:
                    continue
                plugin_id = str(r.get("plugin_id") or "").strip() or str(r["full_name"]).split("/")[-1]
                if os.path.isdir(os.path.join(base_dir, plugin_id)):
                    continue
                self._catalog_db_execute(
                    "DELETE FROM repos WHERE full_name=?", (r["full_name"],)
                )
                removed += 1

            # 4. VERSION 판별 (rate 보호: 저장소 20개 초과 시 24시간 내 검증 결과 재사용)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            repo_rows = self._catalog_db_query(
                "SELECT full_name, default_branch, is_valid, last_checked, source, base_url FROM repos"
            )
            rows_to_check = []
            if len(repo_rows) > _CATALOG_VERIFY_MAX_REPOS:
                for r in repo_rows:
                    if not r.get("last_checked"):
                        rows_to_check.append(r)
                        continue
                    try:
                        # Python 3.10 이하 fromisoformat은 'Z' 미지원 → +00:00 치환, naive면 UTC 부여
                        parsed = datetime.fromisoformat(
                            str(r["last_checked"]).replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - parsed).total_seconds() >= _CATALOG_VERIFY_TTL_SECONDS:
                            rows_to_check.append(r)
                    except Exception:
                        rows_to_check.append(r)
            else:
                rows_to_check = repo_rows

            for r in rows_to_check:
                full_name = r["full_name"]
                is_valid, plugin_id, version, plugin_name = self._catalog_check_repo_version(
                    full_name,
                    r.get("default_branch") or "main",
                    source=str(r.get("source") or "github"),
                    base_url=r.get("base_url"),
                    db_type=db_type,
                )
                self._catalog_db_execute(
                    "UPDATE repos SET is_valid=?, plugin_id=?, latest_version=?, plugin_name=?, last_checked=? WHERE full_name=?",
                    (is_valid, plugin_id or str(full_name).split("/")[-1], version, plugin_name, now, full_name),
                )

            # 5. 완료 상태 기록
            self._catalog_set_meta("last_refresh", now)
            self._catalog_set_meta("refresh_state", "idle")
            self._catalog_set_meta("refresh_error", "")
            cleaned = ", 미설치 정리 {0}개".format(removed) if removed else ""
            return True, "카탈로그가 갱신되었습니다. (토픽 {0}개, 저장소 {1}개{2})".format(
                len(topics), len(merged), cleaned
            )
        except Exception as e:
            try:
                self._catalog_set_meta("refresh_state", "error")
                self._catalog_set_meta("refresh_error", str(e)[:500])
            except Exception:
                pass
            raise

    def _catalog_manual_refresh(self, db_type):
        """catalog_refresh 액션 — 백그라운드 스레드로 즉시 1회 갱신 후
        자동 업데이트가 ON이면 설치 플러그인 일괄 갱신까지 실행 (응답은 즉시)."""
        def _run():
            try:
                self._catalog_refresh_once(db_type)
            finally:
                # 수동 갱신 후에도 설정(PM_AUTO_UPDATE)에 따라 자동 업데이트 수행
                self._catalog_run_auto_update(db_type)
        try:
            self._ensure_catalog_thread(db_type)
            t = threading.Thread(
                target=_run,
                daemon=True, name="pm-catalog-manual-refresh",
            )
            t.start()
            return True, "카탈로그 갱신(+자동 업데이트)을 시작했습니다. (완료까지 수십 초 소요)"
        except Exception as e:
            return False, "카탈로그 갱신 시작 실패: {0}".format(e)

    def _catalog_background_loop(self, db_type):
        """백그라운드 갱신 루프 (daemon). 빈 DB면 즉시 1회, 이후 last_refresh 기준
        interval 경과 시 갱신. sleep은 60초 단위로 쪼개 매번 경과 시간을 체크하므로
        서버 재시작(스레드 재기동)에도 타이머가 0부터 리셋되지 않는다.

        - 루프 시작: last_refresh 읽어 (now - last_refresh) >= interval 이면 즉시 1회 갱신
        - 이후: 60초 sleep 후 last_refresh 기준 경과 재확인 → 경과 시 refresh
        - 사망 복구: 루프가 어떤 예외로든 종료되면 _CATALOG_THREAD_ALIVE를 False로 리셋 —
          다음 _ensure_catalog_thread 호출에서 is_alive()가 False임을 확인하고 재시작한다.
        """
        global _CATALOG_THREAD_ALIVE
        try:
            self._catalog_init_db()
            rows = self._catalog_db_query("SELECT COUNT(*) AS c FROM repos")
            if not rows or int(rows[0]["c"] or 0) == 0:
                self._catalog_refresh_once(db_type)
        except Exception:
            pass
        try:
            while True:
                # 60초 단위 sleep — 매 주기 last_refresh 기준 경과 체크 (재시작 견고)
                time.sleep(_CATALOG_LOOP_TICK_SECONDS)
                try:
                    interval = self._catalog_get_interval_hours(db_type)
                    due = self._catalog_due_for_refresh(interval)
                except Exception:
                    due = True
                if not due:
                    continue
                # 실패 쿨다운: 마지막 실패(refresh_error 기록 시각) 이후
                # _CATALOG_RETRY_COOLDOWN_SECONDS가 지나지 않았으면 재시도 보류.
                try:
                    meta = self._catalog_read_meta()
                    if meta.get("refresh_state") == "error" and meta.get("refresh_error"):
                        last_err_raw = meta.get("last_refresh_error_at") or ""
                        if last_err_raw:
                            last_err = datetime.fromisoformat(
                                last_err_raw.replace("Z", "+00:00")
                            )
                            if last_err.tzinfo is None:
                                last_err = last_err.replace(tzinfo=timezone.utc)
                            if (
                                datetime.now(timezone.utc) - last_err
                            ).total_seconds() < _CATALOG_RETRY_COOLDOWN_SECONDS:
                                continue  # 아직 쿨다운 중 — 다음 틱에 재확인
                except Exception:
                    pass  # 쿨다운 판정 실패 시 안전하게 갱신 진행
                try:
                    self._catalog_refresh_once(db_type)
                    # 카탈로그 갱신 직후 — 자동 업데이트 ON이면 설치 플러그인 일괄 갱신
                    self._catalog_run_auto_update(db_type)
                except Exception:
                    # 실패 시각을 기록해 다음 재시도를 쿨다운 (rate limit 악순환 방지)
                    try:
                        self._catalog_set_meta(
                            "last_refresh_error_at",
                            datetime.now(timezone.utc)
                            .isoformat(timespec="seconds")
                            .replace("+00:00", "Z"),
                        )
                    except Exception:
                        pass
        finally:
            _CATALOG_THREAD_ALIVE = False  # 루프 종료(사망) 시 재시작 가능하도록 리셋

    def _catalog_due_for_refresh(self, interval_hours):
        """last_refresh 기준 interval 경과 여부 — 재시작 후에도 정확 (타이머 리셋 무관).
        last_refresh 없음(최초)이면 즉시 갱신 대상. 반환: (경과 시간 초, 경과 여부)
        """
        try:
            interval_sec = max(60, int(interval_hours) * 3600)
        except Exception:
            interval_sec = max(60, _CATALOG_DEFAULT_INTERVAL_HOURS * 3600)
        try:
            meta = self._catalog_read_meta()
            raw = meta.get("last_refresh") or ""
            if not raw:
                return interval_sec, True
            last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if meta.get("refresh_state") == "error":
                # 실패 상태면 interval 무관하게 재시도 대상 — daemon의 쿨다운 게이트
                # (10분)가 실제 동작하도록 due=True 반환. 최근 성공 직후 실패 시
                # last_refresh가 최신이라 6시간 동안 error가 방치되는 문제 해결.
                return elapsed, True
            return elapsed, elapsed >= interval_sec
        except Exception:
            # 파싱 불가/오류 시 안전하게 즉시 갱신 (스택 방지)
            return interval_sec, True

    def _catalog_thread_is_alive(self):
        """백그라운드 스레드 실제 생존 여부 (전역 참조 스레드의 is_alive)"""
        global _CATALOG_THREAD
        t = _CATALOG_THREAD
        return bool(t is not None and t.is_alive())

    def _ensure_catalog_thread(self, db_type):
        """백그라운드 갱신 스레드 보장 — 살아있으면 no-op, 죽었으면(사망/미기동) 재시작.
        사망 시 _CATALOG_THREAD_ALIVE=False 리셋으로 재시작 가능 (플래그 잔존 버그 제거).
        """
        global _CATALOG_THREAD, _CATALOG_THREAD_ALIVE
        with _CATALOG_THREAD_LOCK:
            if self._catalog_thread_is_alive():
                return
            _CATALOG_THREAD_ALIVE = True
        try:
            t = threading.Thread(
                target=self._catalog_background_loop, args=(db_type,),
                daemon=True, name="pm-catalog-refresh",
            )
            _CATALOG_THREAD = t
            t.start()
        except Exception:
            _CATALOG_THREAD_ALIVE = False

    # ---- 응답 병합 ----

    def _catalog_meta_dict(self, db_type):
        """catalog_meta 응답 — 설정은 MariaDB에서, 상태는 catalog.db meta에서"""
        meta = self._catalog_read_meta()
        refresh_state = meta.get("refresh_state", "idle")
        if refresh_state == "running":
            started_raw = meta.get("refresh_started_at") or ""
            is_stale = False
            if started_raw:
                try:
                    started_dt = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - started_dt).total_seconds() > 300:
                        is_stale = True
                except Exception:
                    is_stale = True
            else:
                is_stale = True

            if is_stale:
                refresh_state = "idle"
                try:
                    self._catalog_set_meta("refresh_state", "idle")
                except Exception:
                    pass

        # Gitea 서버 목록 — 토큰은 실제 값 대신 마스킹 표시 (보안: 프론트로 내려주지 않음)
        gitea_servers = []
        for s in self._catalog_get_gitea_servers(db_type):
            token = s.get("token") or ""
            gitea_servers.append({
                "url": s["url"],
                "host": s["host"],
                "token": (token[:4] + "****") if token else "",
            })

        return {
            "last_refresh": meta.get("last_refresh"),
            "refresh_interval_hours": self._catalog_get_interval_hours(db_type),
            "topics": self._catalog_get_topics(db_type),
            "refresh_state": refresh_state,
            "refresh_error": meta.get("refresh_error") or None,
            "allow_invalid_install": self._catalog_get_allow_invalid_install(db_type),
            "auto_update": self._catalog_get_auto_update(db_type),
            "github_token_set": bool(self._catalog_get_github_token(db_type)),
            "gitea_servers": gitea_servers,
        }

    def _catalog_get_allow_invalid_install(self, db_type):
        """검증 실패 플러그인 설치 허용 설정 (기본 OFF — 미설정이면 안전 기본값)"""
        try:
            gateway = self.get_db_gateway(db_type)
            raw = gateway.get_setting("PM_ALLOW_INVALID_INSTALL", default=None)
            if isinstance(raw, dict):
                raw = raw.get("value")
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    def _catalog_get_auto_update(self, db_type):
        """플러그인 자동 업데이트 설정 (기본 OFF — 실서버 자동 교체 기본 차단)"""
        try:
            gateway = self.get_db_gateway(db_type)
            raw = gateway.get_setting("PM_AUTO_UPDATE", default=None)
            if isinstance(raw, dict):
                raw = raw.get("value")
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    def _catalog_run_auto_update(self, db_type):
        """자동 업데이트 실행 — ON일 때만 일괄 업데이트. 실패해도 상위 갱신 루프를 깨지 않게 이중 격리."""
        try:
            if self._catalog_get_auto_update(db_type):
                self._update_all_plugins(db_type)
        except Exception:
            logger.warning("플러그인 자동 업데이트 실행 중 오류 (%s)", db_type, exc_info=True)

    def _catalog_list_valid_repos(self):
        """is_valid=valid 저장소 목록 (설치 여부 판정용, pushed_at 최신순)"""
        try:
            return self._catalog_db_query(
                "SELECT full_name, html_url, description, topics, default_branch, pushed_at, "
                "plugin_id, plugin_name, latest_version, last_checked, install_error, source, base_url FROM repos "
                "WHERE is_valid='valid' ORDER BY COALESCE(pushed_at, '') DESC"
            )
        except Exception:
            return []

    @staticmethod
    def _catalog_parse_topics_json(raw):
        try:
            data = json.loads(raw or "[]")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _merge_catalog_plugins(self, plugins, db_type):
        """
        설치된 플러그인 목록에 미설치 카탈로그 항목(valid만) 병합.
        설치 여부는 응답 시점에 폴더+id 기준 동적 판정 (DB에 저장 안 함 — 설치/삭제 즉시 반영).
        invalid 저장소는 목록/count에서 제외.
        """
        installed_ids = {p.get("id") for p in plugins if p.get("id")}
        catalog_rows = self._catalog_list_valid_repos()
        merged = list(plugins)
        for r in catalog_rows:
            plugin_id = str(r.get("plugin_id") or "").strip() or str(r["full_name"]).split("/")[-1]
            if plugin_id in installed_ids:
                continue  # 이미 설치됨
            source = str(r.get("source") or "github")
            if source == "gitea" and r.get("base_url"):
                git_url = r.get("html_url") or ("{0}/{1}".format(r["base_url"], r["full_name"]))
            else:
                git_url = r.get("html_url") or ("https://github.com/" + r["full_name"])
            merged.append({
                "id": plugin_id,
                "name": str(r.get("plugin_name") or "").strip() or plugin_id,
                "version": None,
                "latest_version": r.get("latest_version"),
                "has_update": False,
                "enabled": False,
                "is_searchable": False,
                "is_category": False,
                "is_widget": False,
                "has_update_manifest": False,
                "has_config": False,
                "is_system": False,
                "is_installed": False,
                "git_url": git_url,
                "install_error": r.get("install_error"),
                "catalog": {
                    "full_name": r["full_name"],
                    "html_url": r.get("html_url"),
                    "description": r.get("description"),
                    "topics": self._catalog_parse_topics_json(r.get("topics")),
                    "default_branch": r.get("default_branch"),
                    "last_checked": r.get("last_checked"),
                    "source": source,
                    "base_url": r.get("base_url"),
                },
            })
        return merged, self._catalog_meta_dict(db_type)

    # ---- 설정 저장 (자체 save-config API — 코어 .plugin-config-form 우회) ----

    def _catalog_save_config(self, item_data, db_type):
        """
        save_config — 간격(1~24 클램프)/토픽(정규화) 검증 후 gateway.set_setting → MariaDB.
        catalog.db에는 저장하지 않음 (설정과 조회 결과 저장소 분리).
        """
        try:
            item_data = item_data or {}
            gateway = self.get_db_gateway(db_type)

            interval_raw = item_data.get("refresh_interval_hours")
            if interval_raw is None or str(interval_raw).strip() == "":
                interval = _CATALOG_DEFAULT_INTERVAL_HOURS
            else:
                interval = self._catalog_clamp_interval(interval_raw)
            gateway.set_setting("PM_CATALOG_REFRESH_HOURS", str(interval))

            topics = self._catalog_normalize_topics(item_data.get("topics"))
            gateway.set_setting("PM_CATALOG_TOPICS", ",".join(topics))

            allow_raw = item_data.get("allow_invalid_install")
            if allow_raw is None or str(allow_raw).strip() == "":
                allow_val = self._catalog_get_allow_invalid_install(db_type)
            else:
                allow_val = str(allow_raw).strip().lower() in ("1", "true", "yes", "on")
            gateway.set_setting("PM_ALLOW_INVALID_INSTALL", "1" if allow_val else "0")

            auto_raw = item_data.get("auto_update")
            if auto_raw is None or str(auto_raw).strip() == "":
                auto_val = self._catalog_get_auto_update(db_type)
            else:
                auto_val = str(auto_raw).strip().lower() in ("1", "true", "yes", "on")
            gateway.set_setting("PM_AUTO_UPDATE", "1" if auto_val else "0")

            # Gitea 서버 목록 — 프론트가 보낸 마스킹 토큰(****)은 기존 값 유지
            gitea_raw = item_data.get("gitea_servers")
            if gitea_raw is not None:
                existing = {s["host"]: s["token"] for s in self._catalog_get_gitea_servers(db_type)}
                validated = []
                for item in (gitea_raw if isinstance(gitea_raw, list) else []):
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url") or "").strip().rstrip("/")
                    if not url.startswith("https://"):
                        continue
                    host = re.sub(r"^https?://", "", url).split("/")[0]
                    if not host:
                        continue
                    token = str(item.get("token") or "").strip()
                    # 마스킹 표시면 기존 토큰 유지, 실제 값이면 교체
                    if token and "***" in token:
                        token = existing.get(host, "")
                    validated.append({"url": url, "token": token})
                # host 중복 제거
                seen = set()
                dedup = []
                for item in validated:
                    host = re.sub(r"^https?://", "", item["url"]).split("/")[0]
                    if host in seen:
                        continue
                    seen.add(host)
                    dedup.append(item)
                gateway.set_setting(
                    "PM_CATALOG_GITEA_SERVERS",
                    json.dumps(dedup, ensure_ascii=False),
                )

            # GitHub 토큰 — 비어 있으면 기존 유지(보안: 실제 토큰을 프론트로 내려주지 않음),
            # clear_github_token=true면 삭제, 값이 있으면 새로 저장.
            if item_data.get("clear_github_token"):
                gateway.set_setting("PM_GITHUB_TOKEN", "")
            else:
                raw_token = item_data.get("github_token")
                if raw_token and str(raw_token).strip():
                    gateway.set_setting("PM_GITHUB_TOKEN", str(raw_token).strip())

            return True, (
                "설정이 저장되었습니다. (갱신 간격 {0}시간, 토픽 {1}개, 검증 실패 설치 {2}, 자동 업데이트 {3}, GitHub 토큰 {4} — 다음 갱신 주기부터 적용)"
            ).format(
                interval, len(topics), "허용" if allow_val else "차단",
                "ON" if auto_val else "OFF",
                "삭제됨" if item_data.get("clear_github_token")
                else ("등록됨" if (item_data.get("github_token") and str(item_data.get("github_token")).strip()) else "유지"),
            )
        except Exception as e:
            return False, "설정 저장 실패: {0}".format(e)

    def _save_config_route(self):
        """POST /api/media/dashboard/widgets/plugin_manager/save-config (url_map.add 직접 등록)"""
        from flask import jsonify, request
        try:
            payload = request.get_json(silent=True) or {}
            db_type = str(payload.get("type") or "general").strip() or "general"
            ok, msg = self._catalog_save_config(payload, db_type)
            if not ok:
                return jsonify({"success": False, "error": msg})
            return jsonify({
                "success": True,
                "message": msg,
                "catalog_meta": self._catalog_meta_dict(db_type),
            })
        except Exception as e:
            return jsonify({"success": False, "error": "설정 저장 실패: {0}".format(e)})

    def _ensure_catalog_routes(self):
        """save-config 자체 라우트 1회 등록 (함정 2: add_url_rule 대신 url_map.add 직접 등록)"""
        global _CATALOG_ROUTES_REGISTERED
        with _CATALOG_ROUTES_LOCK:
            if _CATALOG_ROUTES_REGISTERED:
                return
            try:
                from flask import current_app
                from werkzeug.routing import Rule
                app = current_app._get_current_object()
                endpoint = "plugin_manager_save_config"
                if endpoint not in app.view_functions:
                    app.url_map.add(Rule(
                        "/api/media/dashboard/widgets/plugin_manager/save-config",
                        endpoint=endpoint, methods=["POST"],
                    ))
                    app.view_functions[endpoint] = self._save_config_route
                _CATALOG_ROUTES_REGISTERED = True
            except Exception:
                pass  # 첫 요청 이전 컨텍스트 부재 등 — 다음 호출에서 재시도

# ── 플러그인 로드 시 백그라운드 카탈로그 갱신 스레드 자동 시작 (2026-08-14) ──
# 코어는 플러그인을 lazy import — 첫 요청이 오면 모듈이 로드되는데, 그 시점에 스레드를
# 시작하면 카탈로그 화면을 직접 열지 않아도 주기 갱신이 보장된다. 기존 get_dashboard_data의
# _ensure_catalog_thread 호출은 _CATALOG_THREAD_STARTED 플래그 때문에 no-op — 중복 시작 없음.
# 테스트 하네스(verify_catalog/verify_sources_db)는 exec로 실행하므로 _PM_SKIP_AUTO_START로 건너뜀.
if not globals().get("_PM_SKIP_AUTO_START", False):
    try:
        PluginManagerMetadataProvider()._ensure_catalog_thread("general")
    except Exception:
        pass  # import 실패로 플러그인 로드 전체가 죽는 것 방지
