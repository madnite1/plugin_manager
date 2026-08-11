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
        "show_sample_update_button": True,
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

                # 4. 업데이트 체크 (update_manifest 기반만)
                has_update, latest_version = self._check_plugin_update(plugin_id, version, cls_obj)

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

    def _resolve_update_base_url(self, plugin_id, raw_base_url):
        """업데이트 소스 URL 결정: GitHub 릴리즈 태그 우선, 없으면 브랜치(raw_base_url) 폴백."""
        try:
            git_info = self._read_git_source_info(plugin_id)
            repo = self._parse_github_repo(git_info.get("git_url")) if git_info else None
            if repo:
                tag = self._fetch_latest_release_tag(repo[0], repo[1])
                if tag:
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

            base_url = self._resolve_update_base_url(plugin_id, spec["raw_base_url"])
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

            return True, f"ZIP 압축 파일을 통해 '{plugin_id}' 플러그인이 성공적으로 설치 및 활성화되었습니다!"

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

            # 1. ZIP 다운로드 (기본 브랜치 main → 실패 시 master 폴백)
            zip_bytes = None
            last_err = None
            for cand_url in self._zip_url_candidates(zip_url):
                try:
                    req = Request(cand_url, headers={"User-Agent": "BookOasis/1.0"})
                    with urlopen(req, timeout=60) as resp:
                        zip_bytes = resp.read()
                    break
                except HTTPError as e:
                    last_err = f"{e.code} {e.reason}"
                except Exception as e:
                    last_err = str(e)
            if not zip_bytes:
                return False, f"저장소 ZIP 다운로드 실패: {last_err}"

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
                        or rel_clean.startswith("\\") or rel_clean in (".", "")):
                    return False, f"update_manifest 에 유효하지 않은 파일 경로가 포함되어 있습니다: {rel}"

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

            return True, (
                f"Git 저장소에서 '{plugin_id}' 플러그인이 성공적으로 설치 및 활성화되었습니다! "
                f"(update_manifest 기준 {len(manifest_files)}개 파일만 유지)"
            )

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
        base_url = self._resolve_update_base_url(plugin_id, spec["raw_base_url"])
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
