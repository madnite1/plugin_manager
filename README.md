# 플러그인 매니저 (plugin_manager)

> 저장소: [github.com/madnite1/plugin_manager](https://github.com/madnite1/plugin_manager)

BookOasis 메타데이터 플러그인을 웹 UI에서 직접 설치·업데이트·삭제·활성화 관리하는 시스템 플러그인입니다.
**ZIP 파일 업로드 설치**, **Git 저장소 URL 설치**, **릴리즈 태그 우선 자동 업데이트**(GitHub 릴리즈 → 브랜치 폴백)를 지원합니다.

---

## 설치

플러그인 매니저 자신도 일반 플러그인과 동일한 방식으로 설치합니다.

- **Git URL 설치** — 플러그인 매니저(또는 대상 서버의 설치 UI)의 "Git 저장소 URL 설치"에 아래 주소 입력:

  ```text
  https://github.com/madnite1/plugin_manager
  ```

- **ZIP 업로드 설치** — 저장소 소스를 ZIP 으로 묶어 업로드 (루트에 `update_manifest` 필수)

설치 후 자동 업데이트는 `update_manifest` 의 `raw_base_url`(GitHub raw)을 통해 동작합니다.

---

## 주요 기능

- **ZIP 업로드 설치** — 브라우저에서 `.zip` 파일 선택 → Base64 전송 → 서버 해제 후 설치
- **Git 저장소 URL 설치** — GitHub/Gitea 저장소 URL 입력 → 소스 ZIP 다운로드 → `update_manifest` 기준 파일만 남기고 설치 (git 바이너리 불필요)
- **플러그인 목록 조회** — 설치된 전체 플러그인의 버전, 활성화 상태, 업데이트 가능 여부, 설정 보유 여부 수집
- **개별/일괄 업데이트** — `update_manifest` 선언된 플러그인에 대해 릴리즈 태그 우선(없으면 브랜치 raw) 최신 버전 다운로드·교체 후 hot reload
- **활성화 토글** — `PLUGIN_ENABLED_<id>` 설정값으로 온/오프, hot reload 즉시 반영
- **삭제** — 플러그인 디렉토리 영구 삭제 (시스템 플러그인 `plugin_manager` 자신은 삭제 불가)
- **설정 모달** — 플러그인 `config_schema` 또는 커스텀 `settings_ui.html` 렌더링 후 저장
- **자동 업데이트 감지** — GitHub 소스 플러그인은 `/releases/latest` 리다이렉트로 최신 릴리즈 태그 감지 (API 키 불필요), 태그 없으면 GitHub raw `VERSION` 브랜치 체크
- **플러그인 카탈로그** — 설정된 GitHub 토픽(기본 `bookoasis-plugin`) 검색으로 공개 BookOasis 플러그인을 자동 수집, 설정된 간격(1~24시간, 기본 6시간)으로 백그라운드 갱신. 설치/미설치 통합 목록 + 필터 탭, 미설치 카드의 **설치** 버튼으로 원클릭 설치 (기존 `install_git` 재사용). 조회 결과는 `catalog.db`(SQLite), 설정(간격/토픽)은 코어 DB(MariaDB)에 저장

---

## 플러그인 토픽 등록 (자동 발견)

BookOasis 호환 플러그인 저장소는 **GitHub 토픽**을 달아두면 자동으로 발견됩니다.
플러그인 매니저 카탈로그는 기본적으로 `topic:bookoasis-plugin` 검색으로
BookOasis 플러그인 목록을 수집하므로, **공개 배포하는 모든 플러그인 저장소**에
아래 토픽을 등록해 주세요. (등록 토픽은 플러그인 매니저 ⚙ 설정에서 추가/변경 가능)

| 토픽 | 용도 |
| :--- | :--- |
| `bookoasis-plugin` | BookOasis 플러그인 여부/유형 (필수) |

### 웹 UI로 등록

1. 저장소 페이지(예: `github.com/<owner>/<repo>`)의 오른쪽 사이드바 **About** 섹션에서 톱니바퀴(⚙) 클릭
2. **Topics** 칸에 `bookoasis-plugin` 입력 후 Enter
3. **Save changes** 클릭

등록 직후 반영되며, 몇 분 내로 검색 인덱스에도 반영됩니다:

```text
https://github.com/search?q=topic%3Abookoasis-plugin&type=repositories
```

### API / CLI로 등록 (참고)

```bash
# GitHub API (PAT 필요)
curl -X PATCH https://api.github.com/repos/{owner}/{repo} \
  -H "Authorization: Bearer ***" \
  -d '{"topics":["bookoasis-plugin"]}'

# GitHub CLI (gh auth login 필요)
gh repo edit <owner>/<repo> --add-topic bookoasis-plugin
```

> 토픽 등록은 플러그인 코드와 무관한 저장소 설정이라 코드 변경/릴리즈가 필요 없습니다.

---

## 파일 구성

```text
plugin_manager/
  .github/workflows/release.yml  # VERSION bump 시 자동 릴리즈 생성 (GitHub Actions)
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

### 소스 다운로드 후보 순서

```
GitHub 소스 + 브랜치 미지정 (예: https://github.com/owner/repo)
  1) 최신 릴리즈 태그 ZIP   ← /releases/latest 리다이렉트로 태그 조회 (API 키 불필요)
  2) main 브랜치 ZIP        ← 태그 없음(릴리즈 미생성) 또는 태그 ZIP 실패 시
  3) master 브랜치 ZIP      ← main 실패 시 폴백
```

예외 조건:

- `/tree/<branch>` 로 브랜치를 **명시한 URL** → 태그 조회 생략, 해당 브랜치 ZIP만 사용
- **Gitea 등 GitHub 이외 호스트** → 태그 조회 자체를 하지 않음 (브랜치 ZIP만, `archive/{branch}.zip`)
- 태그 ZIP 다운로드가 404/오류여도 **자동으로 main → master 로 폴백** (후보 순차 시도 구조)

설치 완료 시 사용된 소스가 표시됩니다 (`릴리즈 태그 1.0.0` 또는 `브랜치 main`).

설치 절차:

1. 위 후보 순서대로 소스 ZIP 다운로드 (표준 라이브러리만 사용, git 바이너리 불필요)
2. 저장소 루트에서 `update_manifest` 를 AST 로 추출 (없으면 설치 거부)
3. `update_manifest.files` 목록에 있는 파일만 남기고 **전부 삭제** (`.git`, `docs/`, 숨김 파일 포함)
4. `plugins/metadata/<plugin_id>` 로 복사 → 소스 메타 저장 → 활성화 + hot reload

설치 시 소스 메타가 `plugin_manager/plugin_sources.db`(sqlite)에 저장됩니다. 설치는 zip/git
어떤 방식이든 **`update_manifest.raw_base_url` 검증 기준**으로 판단합니다 — 유효한 GitHub 루트
주소면 `git_url` / `branch`(릴리즈 태그 설치 시 태그명) / `manifest_files` 이력이 남아 자동
업데이트·GitHub 배지가 활성화되고, manifest가 없거나 monorepo 서브디렉토리면 레코드가 없어
로컬 플러그인으로 유지됩니다 (이전 버전의 `.git_source`/`.zip_source` 파일은 설치 후 최초 1회
자동으로 DB에 마이그레이션됩니다 — `.zip_source`처럼 git_url이 없는 파일은 삭제 후
`update_manifest` 기준으로 재판단).
설치와 업데이트가 같은 소스(릴리즈 태그 우선, 브랜치 폴백)를 바라보므로 버전 불일치가 없습니다.

---

## 업데이트 메커니즘

`update_manifest` 선언으로 자기 자신도 자동 업데이트 가능:

```python
update_manifest = {
    "enabled": True,
    "provider": "github-raw",
    "raw_base_url": "https://raw.githubusercontent.com/madnite1/plugin_manager/main",
    "files": ["plugin_manager.py", "__init__.py", "VERSION", "index.html", "style.css", "script.js", "settings.html", "settings.js"],
    "version_file": "VERSION",
    "version_key": "plugin version",
    "show_sample_update_button": False,
}
```

### 우선순위 (자체 업데이트 엔진, 코어 PluginService 미사용)

1. **릴리즈 태그 (우선)** — 설치 시 저장된 소스 메타(`plugin_sources.db`)의 `git_url`이 GitHub 소스이면
   `/releases/latest` 리다이렉트로 최신 릴리즈 태그를 추출(API 키 불필요, 5분 TTL 캐시)하고
   해당 태그의 raw URL에서 `VERSION` 비교 → 파일 다운로드/교체 → hot reload.
   릴리즈를 안 만든 커밋의 VERSION bump는 무시되므로 개발 중 실수 감지를 방지.
2. **브랜치 (폴백)** — `git_url`이 없거나 태그 조회 실패/릴리즈가 없으면 기존 방식대로
   `raw_base_url`(main 브랜치 raw)의 `VERSION` 파일을 비교해 업데이트.

`enabled: True`는 필수 게이트 — 없으면 업데이트 대상 목록에 포함되지 않습니다.
버전 비교 규칙: SemVer core(`MAJOR.MINOR.PATCH`)만 비교, `v` 접두사·pre-release 접미사 무시,
로컬 < 원격일 때만 업데이트 허용.

---

## GitHub Actions 자동 릴리즈

`.github/workflows/release.yml` — `VERSION` 파일이 `main`에 push되면 자동으로 GitHub Release 생성:

- **트리거**: `main` push + `paths: [VERSION]` (VERSION 변경 시에만 실행, 무한 루프 없음)
- **버전 추출**: `jq`로 VERSION JSON의 `"plugin version"` 값 사용
- **중복 방지**: `git ls-remote`로 동일 태그 존재 시 릴리즈 생성을 스킵
- **릴리즈 본문**: 이전 릴리즈 태그 이후의 커밋 로그(`git log PREV_TAG..HEAD --oneline`)를
  "변경사항"으로 자동 포함 (최초 릴리즈면 전체 커밋)
- **자산**: `update_manifest.files`와 동일한 6개 파일 첨부

> 사전 설정: 저장소 Settings → Actions → General → **Workflow permissions: Read and write**
> (릴리즈/태그 생성 권한 필요)

릴리즈 태그 = main 커밋 스냅샷이므로, 자동 릴리즈된 버전은 plugin_manager의
"릴리즈 태그 우선" 업데이트 경로로 즉시 감지됩니다.

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