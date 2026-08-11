# 플러그인 매니저 (plugin_manager)

> 저장소: [github.com/madnite1/bookoasis_plugin_manager](https://github.com/madnite1/bookoasis_plugin_manager)

BookOasis 메타데이터 플러그인을 웹 UI에서 직접 설치·업데이트·삭제·활성화 관리하는 시스템 플러그인입니다.
**ZIP 파일 업로드 설치**, **Git 저장소 URL 설치**, **update_manifest 기반 자동 업데이트** 세 가지 경로를 지원합니다.

---

## 설치

플러그인 매니저 자신도 일반 플러그인과 동일한 방식으로 설치합니다.

- **Git URL 설치** — 플러그인 매니저(또는 대상 서버의 설치 UI)의 "Git 저장소 URL 설치"에 아래 주소 입력:

  ```text
  https://github.com/madnite1/bookoasis_plugin_manager
  ```

- **ZIP 업로드 설치** — 저장소 소스를 ZIP 으로 묶어 업로드 (루트에 `update_manifest` 필수)

설치 후 자동 업데이트는 `update_manifest` 의 `raw_base_url`(GitHub raw)을 통해 동작합니다.

---

## 주요 기능

- **ZIP 업로드 설치** — 브라우저에서 `.zip` 파일 선택 → Base64 전송 → 서버 해제 후 설치
- **Git 저장소 URL 설치** — GitHub/Gitea 저장소 URL 입력 → 소스 ZIP 다운로드 → `update_manifest` 기준 파일만 남기고 설치 (git 바이너리 불필요)
- **플러그인 목록 조회** — 설치된 전체 플러그인의 버전, 활성화 상태, 업데이트 가능 여부, 설정 보유 여부 수집
- **개별/일괄 업데이트** — `update_manifest` 선언된 플러그인에 대해 GitHub raw URL에서 최신 버전 다운로드·교체 후 hot reload
- **활성화 토글** — `PLUGIN_ENABLED_<id>` 설정값으로 온/오프, hot reload 즉시 반영
- **삭제** — 플러그인 디렉토리 영구 삭제 (시스템 플러그인 `plugin_manager` 자신은 삭제 불가)
- **설정 모달** — 플러그인 `config_schema` 또는 커스텀 `settings_ui.html` 렌더링 후 저장
- **자동 업데이트 감지** — `update_manifest` 기반 신규 버전 체크 (GitHub raw `VERSION` 파일)

---

## 파일 구성

```text
plugin_manager/
  __init__.py          # PluginManagerMetadataProvider export
  plugin_manager.py    # 플러그인 본체 (BaseMetadataProvider 상속)
  VERSION              # 버전 파일 ("plugin version" 키)
  index.html           # 풀페이지 UI 마크업
  style.css            # 풀페이지 UI 스타일
  script.js            # 풀페이지 UI 동작 (목록 로드, 카드 렌더링, 모달, API 호출)
```

외부 파이썬 패키지 의존성 없음 — 표준 라이브러리(`os`, `shutil`, `json`, `re`, `tempfile`, `urllib`, `zipfile`, `base64`)만 사용.

---

## 백엔드 액션 (API)

`/api/media/books/0/apply-metadata` 엔드포인트로 `item_data.action` 전달:

| action | 설명 | 주요 파라미터 |
| :--- | :--- | :--- |
| `install_zip` | ZIP 파일 업로드 설치 | `zip_data` (Base64), `filename` |
| `install_git` | Git 저장소 URL 설치 | `git_url` |
| `update` | 특정 플러그인 업데이트 | `plugin_id` |
| `update_all` | 설치된 전체 플러그인 일괄 업데이트 | — |
| `delete` | 플러그인 디렉토리 영구 삭제 | `plugin_id` |
| `toggle` | 활성화/비활성화 토글 | `plugin_id`, `enabled` (`"1"`/`"0"`) |

데이터 조회는 `/api/media/dashboard/widgets/plugin_manager/data` (전체 목록) 및 `/api/media/metadata/plugins/manage` (설정 모달용 상세) 사용.

---

## 보안

- **경로 이탈 차단** — `_validate_plugin_path` 가 `plugins/metadata/` 경계 밖 접근 엄격 차단
- **Zip Slip 차단** — ZIP 압축 해제 전 상위 경로(`..`, 절대경로) 멤버 검사 (업로드 ZIP/Git 다운로드 ZIP 공통)
- **URL scheme 검증** — Git 설치 시 `http/https` URL만 허용 (`file://`, `git@`, `ssh://` 차단)
- **manifest 경로 검증** — `update_manifest.files` 항목의 상위 경로 이탈(`..`, 절대경로) 사전 차단
- **AST 안전 파싱** — 클론된 플러그인 코드의 `update_manifest` 를 코드 실행 없이 AST 로만 추출
- **플러그인 ID 검증** — `^[a-zA-Z0-9_-]+$` 정규식으로 안전한 ID만 허용
- **시스템 플러그인 보호** — `plugin_manager` 자신은 삭제/덮어쓰기 불가
- **플러그인 핫 리로드** — 설치/삭제/토글/업데이트 후 `MetadataFactory.hot_reload_plugin()` 즉시 호출

---

## Git 저장소 URL 설치

지원 URL 형식 (단독 플러그인 저장소 = 저장소 루트가 플러그인 자체):

```text
https://github.com/<owner>/<repo>[/tree/<branch>]     # GitHub (기본 브랜치 main, 실패 시 master 폴백)
https://github.com/<owner>/<repo>.git                  # .git 형식
https://<host>/<org>/<repo>[/src/branch/<branch>]      # Gitea 등 (archive/{branch}.zip 방식)
```

설치 절차:

1. 저장소 소스를 HTTP ZIP 으로 다운로드 (`codeload.github.com` / `archive/{branch}.zip` — 표준 라이브러리만 사용, git 바이너리 불필요)
2. 저장소 루트에서 `update_manifest` 를 AST 로 추출 (없으면 설치 거부)
3. `update_manifest.files` 목록에 있는 파일만 남기고 **전부 삭제** (`.git`, `docs/`, 숨김 파일 포함)
4. `plugins/metadata/<plugin_id>` 로 복사 → `.git_source` 메타 저장 → 활성화 + hot reload

설치 시 `.git_source` 파일이 생성되어 `source_type` / `git_url` / `branch` / `manifest_files` 이력이 남습니다.

---

## 업데이트 메커니즘

`update_manifest` 선언으로 자기 자신도 자동 업데이트 가능:

```python
update_manifest = {
    "enabled": True,
    "provider": "github-raw",
    "raw_base_url": "https://raw.githubusercontent.com/madnite1/bookoasis_plugin_manager/main",
    "files": ["plugin_manager.py", "__init__.py", "VERSION", "index.html", "style.css", "script.js"],
    "version_file": "VERSION",
    "version_key": "plugin version",
    "show_sample_update_button": True,
}
```

버전 비교는 `PluginService.can_update_to_github_version(local, remote)` 사용.
GitHub raw `VERSION` 파일을 직접 호출해 최신 버전 감지.

---

## 클래스 속성

| 속성 | 값 |
| :--- | :--- |
| `id` | `"plugin_manager"` |
| `name` | `"플러그인 매니저"` |
| `is_searchable` | `False` (수동 메타데이터 검색 미지원) |
| `config_schema` | `[]` (빈 스키마) |
| `category_tab` | `{"title": "플러그인 매니저", "icon": "fa-solid fa-boxes-stacked", "order": 99}` |

`BaseMetadataProvider` 상속. `search()` 및 `apply()` 오버라이드.