# -*- coding: utf-8 -*-
import os
import sys
import ast
import shutil
import json
import re
import tempfile
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import logging
from plugins.metadata.base import BaseMetadataProvider

logger = logging.getLogger(__name__)

# GitHub 릴리즈 태그 조회 TTL 캐시 (releases/latest 리다이렉트는 요청마다 수행하면 느리므로 5분 캐시)
_RELEASE_TAG_CACHE = {}
_RELEASE_TAG_CACHE_TTL = 300  # 초


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
        "files": ["plugin_manager.py", "__init__.py", "VERSION", "index.html", "style.css", "script.js"],
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
            if not zip_data:
                return False, "ZIP 압축 파일 데이터가 누락되었습니다."
            return self._install_from_zip(zip_data, filename, db_type)

        elif action == "install_git":
            git_url = str(item_data.get("git_url", "")).strip()
            if not git_url:
                return False, "Git 저장소 URL이 누락되었습니다."
            return self._install_from_git(git_url, db_type)

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

        return False, f"지원하지 않는 액션입니다: {action}"

    def get_dashboard_data(self, db_type, limit=10):
        """
        플러그인 목록 조회 API
        """
        try:
            plugins = self._list_plugins(db_type)
            return {
                "success": True,
                "plugins": plugins,
                "count": len(plugins),
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
                category_tab = getattr(cls_obj, "category_tab", None) if cls_obj else None
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

                # 4-1. Git 소스 메타 (설치 경로 표시용 — .git_source의 source_type == git_url)
                git_url = None
                git_info = self._read_git_source_info(plugin_id)
                if git_info and git_info.get("source_type") == "git_url":
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
        """플러그인 설치 시 저장된 .git_source 메타 읽기 (없으면 None)"""
        try:
            pdir = os.path.join(self._get_plugins_base_dir(), plugin_id)
            path = os.path.join(pdir, ".git_source")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _parse_github_repo(self, git_url):
        """GitHub 저장소 URL에서 (owner, repo) 추출. GitHub가 아니면 None."""
        url = str(git_url or "").strip().rstrip("/")
        url = re.sub(r"\.git$", "", url)
        m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:/tree/([^/]+))?$", url)
        if m:
            return m.group(1), m.group(2)
        return None

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

    def _parse_raw_base_url(self, raw_base_url):
        """raw.githubusercontent.com URL에서 (owner, repo, branch, subpath) 추출.
        subpath: branch 이후의 서브디렉토리 경로 (없으면 '').
        e.g. .../madnite1/plugin_manager/main → ('madnite1', 'plugin_manager', 'main', '')
             .../leeyj/BookOasis_stable/main/plugins/metadata/stats_dashboard → ('leeyj', 'BookOasis_stable', 'main', 'plugins/metadata/stats_dashboard')
        """
        url = str(raw_base_url or "").strip().rstrip("/")
        m = re.match(
            r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)(/.*)?$", url
        )
        if m:
            subpath = (m.group(4) or "").strip("/")
            return m.group(1), m.group(2), m.group(3), subpath
        return None

    def _ensure_git_source_from_raw_base_url(self, plugin_id, raw_base_url, manifest_files):
        """.git_source 파일이 없을 때, raw_base_url에서 GitHub 정보를 추론하여
        git 설치 시와 동일한 형태의 .git_source 파일을 생성한다.
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
            git_url = f"https://github.com/{owner}/{repo}"
            git_source_info = {
                "source_type": "git_url",
                "git_url": git_url,
                "branch": branch,
                "installed_at": datetime.now().isoformat(),
                "manifest_files": manifest_files or [],
            }
            pdir = os.path.join(self._get_plugins_base_dir(), plugin_id)
            with open(os.path.join(pdir, ".git_source"), "w", encoding="utf-8") as f:
                json.dump(git_source_info, f, indent=2, ensure_ascii=False)
            return git_source_info
        except Exception:
            return None

    def _resolve_update_base_url(self, plugin_id, raw_base_url, manifest_files=None):
        """업데이트 소스 URL 결정: GitHub 릴리즈 태그 우선, 없으면 브랜치(raw_base_url) 폴백.
        .git_source 파일이 없으면 raw_base_url에서 추론하여 생성한 뒤 동일하게 처리."""
        try:
            git_info = self._read_git_source_info(plugin_id)
            if not git_info:
                git_info = self._ensure_git_source_from_raw_base_url(
                    plugin_id, raw_base_url, manifest_files
                )
            repo = self._parse_github_repo(git_info.get("git_url")) if git_info else None
            if repo:
                tag = self._fetch_latest_release_tag(repo[0], repo[1])
                if tag:
                    branch = str(git_info.get("branch") or "").strip()
                    prefix = f"https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/{branch}"
                    if branch and raw_base_url.startswith(prefix):
                        return raw_base_url.replace(prefix, f"https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/{tag}", 1)
                    return f"https://raw.githubusercontent.com/{repo[0]}/{repo[1]}/{tag}"
        except Exception:
            pass
        return raw_base_url

    def _fetch_text(self, url, timeout=15):
        """URL GET → 텍스트 (UTF-8, 오류 시 예외 전파)"""
        req = Request(url, headers={"User-Agent": "BookOasis/1.0"})
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

    def _fetch_remote_plugin_version(self, base_url, version_file="VERSION", version_key="plugin version"):
        """원격 VERSION 조회 (실패/파싱 불가 시 None — 체크는 조용히 실패)"""
        try:
            url = f"{base_url.rstrip('/')}/{version_file}"
            return self._parse_remote_version(self._fetch_text(url), version_key)
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

        has_update, latest_version = self._check_plugin_update(plugin_id, version, cls_obj)
        return True, {
            "plugin_id": plugin_id,
            "version": version,
            "has_update": has_update,
            "latest_version": latest_version,
        }

    def _check_plugin_update(self, plugin_id, local_version, cls_obj):
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
                plugin_id, spec["raw_base_url"], spec.get("files")
            )
            remote_ver = self._fetch_remote_plugin_version(
                base_url,
                version_file=spec["version_file"],
                version_key=spec["version_key"],
            )
            if remote_ver and self._can_update_to_version(local_version, remote_ver):
                return True, remote_ver
        except Exception:
            pass

        return has_update, latest_version

    def _install_from_zip(self, zip_data_b64, filename, db_type):
        """Zip 압축 파일 업로드를 통한 플러그인 설치"""
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
            if not source_ok:
                failed_items = [f"- {c['name']}: {c['detail']}" for c in source_checks if not c.get("ok")]
                return False, (
                    "플러그인 검증 실패 — 설치를 중단했습니다 (기존 폴더는 변경되지 않음):\n"
                    + "\n".join(failed_items)
                )

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

            # ZIP 소스 메타 정보 저장
            zip_source_info = {
                "source_type": "zip_upload",
                "filename": filename or "plugin.zip",
                "installed_at": datetime.now().isoformat()
            }
            with open(os.path.join(dest_dir, ".zip_source"), "w", encoding="utf-8") as f:
                json.dump(zip_source_info, f, indent=2, ensure_ascii=False)

            from services.plugin_service import PluginService
            PluginService.toggle_plugin_enabled('general', plugin_id, "1")

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

    def _install_from_git(self, git_url, db_type):
        """
        GitHub/Gitea 저장소 URL 을 통한 플러그인 설치 (git 바이너리 불필요).

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
            if not manifest_files:
                return False, (
                    "다운로드된 저장소에서 update_manifest.files 목록을 찾을 수 없습니다. "
                    "Git URL 설치는 update_manifest 를 선언한 플러그인만 지원합니다."
                )
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

            # 5. files 목록 경로 안전성 검증 (경로 이탈 차단)
            for rel in manifest_files:
                rel_clean = os.path.normpath(str(rel))
                if (rel_clean.startswith("..") or rel_clean.startswith("/")
                        or rel_clean.startswith("\\\\") or rel_clean in (".", "")):
                    return False, f"update_manifest 에 유효하지 않은 파일 경로가 포함되어 있습니다: {rel}"

            # 5-1. 1차 검증: 정적 소스 검증 (코드 실행 없음 — AST/파일 스캔, zip 설치와 동일 기준)
            #      prune 전에 수행해야 UI 번들/VERSION/symlink 등 전체 파일 기준 검사 가능
            source_ok, source_checks = self._validate_plugin_source(target_plugin_dir, plugin_id)
            if not source_ok:
                failed_items = [f"- {c['name']}: {c['detail']}" for c in source_checks if not c.get("ok")]
                return False, (
                    "플러그인 검증 실패 — 설치를 중단했습니다 (기존 폴더는 변경되지 않음):\n"
                    + "\n".join(failed_items)
                )

            # 6. manifest 목록 외 전부 삭제 (.git 등 포함 안전 처리)
            try:
                self._prune_plugin_dir(target_plugin_dir, manifest_files)
            except Exception as e:
                return False, f"플러그인 파일 정리 중 오류가 발생했습니다: {str(e)}"

            # 7. 이전 디렉토리 교체 후 복사
            if os.path.exists(dest_dir):
                shutil.rmtree(dest_dir)
            shutil.copytree(target_plugin_dir, dest_dir)

            # 8. Git 소스 메타 정보 저장
            git_source_info = {
                "source_type": "git_url",
                "git_url": git_url,
                "branch": branch,
                "installed_at": datetime.now().isoformat(),
                "manifest_files": manifest_files,
            }
            with open(os.path.join(dest_dir, ".git_source"), "w", encoding="utf-8") as f:
                json.dump(git_source_info, f, indent=2, ensure_ascii=False)

            # 9. 활성화 + 핫 리로드
            from services.plugin_service import PluginService
            PluginService.toggle_plugin_enabled('general', plugin_id, "1")

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
                f"(update_manifest 기준 {len(manifest_files)}개 파일만 유지, 검증 통과: {', '.join(passed)})"
            )
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
                    if os.path.isfile(fpath):
                        with open(fpath, "r", encoding="utf-8", errors="replace") as py_f:
                            content = py_f.read()
                            if "BaseMetadataProvider" in content or "id =" in content or "id=" in content:
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

        # 2. Python 코드에서 id = "..." 검색
        try:
            for fname in os.listdir(plugin_dir):
                if fname.endswith(".py") and fname not in ("__init__.py", "base.py"):
                    fpath = os.path.join(plugin_dir, fname)
                    if os.path.isfile(fpath):
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                            match = re.search(r'id\s*=\s*["\']([a-zA-Z0-9_-]+)["\']', content)
                            if match:
                                return match.group(1).strip()
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
                checks.append({"name": "VERSION", "ok": True, "detail": vdetail})
            else:
                checks.append({"name": "VERSION", "ok": False,
                               "detail": "update_manifest 선언 시 VERSION 필수 — " + vdetail})
        elif vfile_ok:
            checks.append({"name": "VERSION", "ok": True, "detail": vdetail})
        else:
            checks.append({"name": "VERSION", "ok": True, "warn": True,
                           "detail": "경고: " + vdetail + " (업데이트 체크 불가)"})

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
            checks.append({"name": "소스", "ok": False, "detail": "메인 .py 파일이 없습니다"})
        elif not provider_found:
            checks.append({"name": "소스", "ok": False,
                           "detail": "BaseMetadataProvider 상속 클래스를 찾을 수 없습니다"})
        else:
            checks.append({"name": "소스", "ok": True, "detail": "%d개 .py 파일, BaseMetadataProvider 클래스 발견" % len(py_files)})

        # 3. 클래스 id 일치 검사 (폴더명/감지 id와 일치해야 목록 표시)
        if class_id is not None:
            if str(class_id).strip() == str(detected_id).strip():
                checks.append({"name": "클래스 id", "ok": True, "detail": class_id})
            else:
                checks.append({"name": "클래스 id", "ok": False,
                               "detail": f"코드 내 id='{class_id}' ≠ 감지된 id='{detected_id}' — 설치 후 목록에 표시되지 않을 수 있습니다"})
        elif provider_found:
            checks.append({"name": "클래스 id", "ok": False,
                           "detail": "플러그인 클래스에 id 속성이 없습니다"})
        else:
            checks.append({"name": "클래스 id", "ok": False, "detail": "클래스를 찾을 수 없어 검사 불가"})

        # 4. 필수 클래스 필드 검사 (가이드: id/name/is_searchable/config_schema)
        if provider_found:
            missing_fields = [f for f in ("name", "is_searchable", "config_schema")
                              if f not in cls_attrs]
            if missing_fields:
                checks.append({"name": "필수 필드", "ok": False,
                               "detail": "클래스에 없음: " + ", ".join(missing_fields)})
            else:
                checks.append({"name": "필수 필드", "ok": True,
                               "detail": "name/is_searchable/config_schema 확인"})
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
                               "detail": "구현 안 됨: " + ", ".join(missing)})
            else:
                checks.append({"name": "필수 메서드", "ok": True, "detail": "search/apply 확인"})
        else:
            checks.append({"name": "필수 메서드", "ok": False, "detail": "클래스 없음"})

        # 6. 금지 패턴 검사
        if forbidden_hits:
            checks.append({"name": "금지 패턴", "ok": False,
                           "detail": "; ".join(forbidden_hits[:3])})
        else:
            checks.append({"name": "금지 패턴", "ok": True, "detail": "eval/exec/subprocess 없음"})

        # __init__.py 존재 여부 (없으면 폴백 로드 — 경고만)
        if os.path.isfile(os.path.join(plugin_dir, "__init__.py")):
            checks.append({"name": "__init__.py", "ok": True, "detail": "확인"})
        else:
            checks.append({"name": "__init__.py", "ok": True, "warn": True,
                           "detail": "경고: __init__.py 없음 (폴백 로드 사용)"})

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
                           "detail": "플러그인 폴더 내 심볼릭 링크 금지: " + ", ".join(symlinks[:3])})
        else:
            checks.append({"name": "심볼릭 링크", "ok": True, "detail": "없음"})

        # 8. update_manifest 규격 검사 (선언된 경우에만)
        if manifest_files:
            problems = []
            m_provider = str(manifest.get("provider") or "").strip()
            if m_provider != "github-raw":
                problems.append("provider='%s' (github-raw만 지원)" % (m_provider or "없음"))
            if not str(manifest.get("version_file") or "").strip():
                problems.append("version_file 없음")
            if not str(manifest.get("version_key") or "").strip():
                problems.append("version_key 없음")
            missing_files = [rel for rel in manifest_files
                             if not os.path.isfile(os.path.join(plugin_dir, rel))]
            if missing_files:
                problems.append("files에 선언된 파일이 없음: " + ", ".join(missing_files[:3]))
            if not str(manifest.get("raw_base_url") or "").strip():
                problems.append("raw_base_url이 비어 있음")
            if problems:
                checks.append({"name": "update_manifest", "ok": False,
                               "detail": "; ".join(problems[:4])})
            else:
                checks.append({"name": "update_manifest", "ok": True,
                               "detail": "provider/files %d개/raw_base_url/version_file/version_key 확인" % len(manifest_files)})
        else:
            checks.append({"name": "update_manifest", "ok": True,
                           "detail": "미선언 (업데이트 미지원)"})

        # 9. UI 번들 검사 (category_tab 선언 시 index.html/script.js/style.css 필수)
        ui_files = {f: os.path.isfile(os.path.join(plugin_dir, f))
                    for f in ("index.html", "script.js", "style.css")}
        if "category_tab" in cls_attrs:
            missing_ui = [f for f, ok in ui_files.items() if not ok]
            if missing_ui:
                checks.append({"name": "UI 번들", "ok": False,
                               "detail": "category_tab 선언 시 필수: " + ", ".join(missing_ui)})
            else:
                checks.append({"name": "UI 번들", "ok": True, "detail": "index/script/style 확인"})
        else:
            present_ui = [f for f, ok in ui_files.items() if ok]
            if present_ui and len(present_ui) < 3:
                checks.append({"name": "UI 번들", "ok": True, "warn": True,
                               "detail": "경고: UI 파일 일부만 존재 (" + ", ".join(present_ui) + ")"})
            elif present_ui:
                checks.append({"name": "UI 번들", "ok": True, "detail": "UI 번들 확인"})
            else:
                checks.append({"name": "UI 번들", "ok": True, "detail": "미선언"})

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
            plugin_id, spec["raw_base_url"], spec.get("files")
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
        downloaded = {}
        for name in spec["files"]:
            file_url = f"{base_url.rstrip('/')}/{name}"
            try:
                downloaded[name] = self._fetch_text(file_url)
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
            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)
            return True, f"플러그인 '{plugin_id}'가 성공적으로 삭제되었습니다."
        except Exception as e:
            return False, f"플러그인 삭제 실패: {str(e)}"

    def _toggle_plugin(self, plugin_id, enabled_val, db_type):
        """플러그인 활성화/비활성화 토글"""
        try:
            from services.plugin_service import PluginService
            ok, err = PluginService.toggle_plugin_enabled('general', plugin_id, str(enabled_val))
            if not ok:
                return False, err or "상태 변경 실패"

            from services.metadata_factory import MetadataFactory
            MetadataFactory.hot_reload_plugin(plugin_id)

            status_text = "활성화" if str(enabled_val) == "1" else "비활성화"
            return True, f"플러그인 '{plugin_id}' 상태가 '{status_text}'로 변경되었습니다."
        except Exception as e:
            return False, f"플러그인 토글 중 오류 발생: {str(e)}"
