# 플러그인 매니저 (plugin_manager)

> 저장소: [github.com/madnite1/plugin_manager](https://github.com/madnite1/plugin_manager)

BookOasis 메타데이터 플러그인을 웹 UI에서 직접 설치·업데이트·삭제·활성화 관리하는 시스템 플러그인입니다.
**ZIP 파일 업로드 설치**, **Git 저장소 URL 설치**, **릴리즈 태그 우선 자동 업데이트**(GitHub 릴리즈 → 브랜치 폴백)를 지원합니다.
**Gitea 카탈로그 서버별 활성화/비활성화 토글**로 개별 서버의 카탈로그 조회/업데이트/저장소 변경 포함 여부를 제어하며,
설정 저장 후 다시 열어도 비활성 서버 항목과 토큰 마스킹 상태를 그대로 복원합니다.

---

## 설치

플러그인 매니저 자신도 일반 플러그인과 동일한 방식으로 설치합니다.

- **Git URL 설치** — 플러그인 매니저(또는 대상 서버의 설치 UI)의 "Git 저장소 URL 설치"에 아래 주소 입력:

  ```text
  https://github.com/madnite1/plugin_manager
  ```

- **ZIP 업로드 설치** — 저장소 소스를 ZIP 으로 묶어 업로드 (루트에 `update_manifest` 필수)
  - 같은 `plugin_id`가 이미 설치되어 있으면 신규 설치가 아니라 **ZIP 업데이트**로 처리합니다.
  - `update_manifest.files`에 선언된 관리 파일만 추가·교체·삭제하고, 목록 밖의 DB·캐시·노드 식별자 등 런타임 데이터는 보존합니다.
  - 업데이트 전에 기존 플러그인 폴더를 임시 백업하고, 핫 리로드/로드 검증 실패 시 기존 버전으로 자동 복원합니다.
  - 기존 Git 소스 정보가 있는 플러그인은 ZIP 업데이트만으로 업데이트 원본 저장소가 바뀌지 않습니다.

설치 후 자동 업데이트는 `update_manifest` 의 `raw_base_url`(GitHub raw)을 통해 동작합니다.

---

## 데이터 영속성 아키텍처

플러그인 매니저는 **플러그인 폴더 밖의 별도 데이터 디렉토리**를 사용해 설정과 카탈로그를 저장합니다. 이를 통해 플러그인 업데이트·삭제·재설치 시에도 데이터가 보존됩니다.

### 디렉토리 구조

```text
BookOasis/
└── plugins/
    ├── metadata/
    │   └── plugin_manager/          ← 플러그인 코드 (업데이트 시 교체)
    │       ├── plugin_manager.py
    │       ├── catalog.db           ← 레거시 위치 (마이그레이션 후 미사용)
    │       └── plugin_sources.db    ← 레거시 위치 (마이그레이션 후 미사용)
    └── data/
        └── plugin_manager/          ← 영속 데이터 (../../data/plugin_manager/)
            ├── catalog.db           # 카탈로그 인덱스(repos, meta) + 설정(settings)
            ├── plugin_sources.db    # 소스 메타 (git_url, branch 등)
            └── .migrated            # 마이그레이션 완료 플래그
```

### 저장되는 설정 키 (catalog.db.settings)

| 키 | 설명 |
|-----|------|
| `PM_CATALOG_GITEA_SERVERS` | Gitea 서버 목록 (URL, 토큰, 활성화 상태) |
| `PM_CATALOG_TOPICS` | 카탈로그 검색 토픽 (쉼표 구분) |
| `PM_CATALOG_REFRESH_HOURS` | 카탈로그 갱신 간격 (1~24시간) |
| `PM_ALLOW_INVALID_INSTALL` | 검증 실패 플러그인 설치 허용 여부 |
| `PM_AUTO_UPDATE` | 플러그인 자동 업데이트 ON/OFF |
| `PM_GITHUB_TOKEN` | GitHub API 토큰 (Bearer 인증용) |

### 특징

- **세션 독립적** — `general`/`adult`/`audiobook`/`video` 등 세션(db_type)과 무관하게 동일 설정 사용
- **업데이트 시 자동 마이그레이션** — 플러그인 업데이트 후 최초 초기화 시 레거시 DB(`plugin_dir/catalog.db`, `plugin_sources.db`)를 새 위치로 복사하고, 코어 DB(MariaDB) 설정도 `catalog.db.settings`로 마이그레이션 (1회만 실행, `.migrated` 플래그로 관리)
- **신규 설치 시** — 빈 DB 자동 생성, 마이그레이션 불필요
- **토큰 보안** — Gitea/GitHub 토큰이 카탈로그 DB에 평문 저장되므로 파일 권한 관리 필요 (Docker 볼륨 마운트 권장)

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

## 저장소 변경 (Replace Source)

설치된 플러그인의 원격 저장소에 문제가 생겨(삭제/이동/접근 불가) 업데이트가 차단된 경우, 카탈로그에 등록된 대체 저장소(GitHub/Gitea)로 **저장소를 변경**할 수 있습니다.

### 동작 조건
- 플러그인이 현재 소스에서 업데이트 불가 상태(`update_blocked: true`)여야 함
- 카탈로그에 동일 플러그인 ID의 다른 소스(GitHub 또는 활성화된 Gitea) 후보가 있어야 함
- 시스템 플러그인(`plugin_manager`)은 제외

### 사용 방법
1. 플러그인 카드에 **"저장소 연결 안됨"** 뱃지와 **"저장소 변경"** 버튼이 표시됨
2. 버튼 클릭 → 모달에서 후보 저장소 선택 (버전, 소스 타입, URL 확인 가능)
3. **"변경하기"** 클릭 → 기존 플러그인 백업 → 선택한 소스에서 재설치 → 성공 시 완료, 실패 시 자동 롤백

### 후보 필터링
- Gitea 서버는 설정 모달에서 **활성화된 서버만** 후보에 포함
- 비활성화된 Gitea 서버의 플러그인은 교체 후보에서 자동 제외

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
- **자산**: `update_manifest.files`와 동일한 8개 파일 첨부 (`settings.html`, `settings.js` 포함)

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