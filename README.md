# 플러그인 매니저 (plugin_manager)

> 저장소: [github.com/madnite1/plugin_manager](https://github.com/madnite1/plugin_manager)

BookOasis 메타데이터 플러그인을 웹 UI에서 직접 설치·업데이트·삭제·활성화 관리하는 시스템 플러그인입니다.
**ZIP 파일 업로드 설치**, **Git 저장소 URL 설치**, **단일 업데이트 경로(branch / release / tag)** 선택을 지원합니다. 기본 업데이트 경로는 저장소 owner 기준으로 결정되며 `madnite1`은 `release`, 그 외는 `branch`입니다. 선택한 경로에서 다른 경로로 자동 폴백하지 않습니다.
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

설치 후 온라인 업데이트는 저장된 Git 소스를 기준으로 **저장소 ZIP을 우선 다운로드**하고, ZIP 내부의 최신 `VERSION`과 `update_manifest.files`를 검증한 뒤 적용합니다. 저장소 ZIP 자체를 받을 수 없는 레거시/특수 소스에 한해서만 기존 raw 파일 다운로드 경로를 호환용으로 사용합니다.

---

## 데이터 영속성 아키텍처

플러그인 매니저는 **플러그인 폴더 밖의 별도 데이터 디렉토리**를 사용해 설정과 카탈로그를 저장합니다. 업데이트·재설치 시에는 데이터가 보존되며, 플러그인 삭제 시에는 `plugins/data/<plugin_id>`가 존재하는 경우 삭제 모달에서 데이터 폴더까지 함께 지울지 선택할 수 있습니다. 선택하지 않으면 영속 데이터는 보존됩니다.

### 디렉토리 구조

```text
BookOasis/
└── plugins/
    ├── metadata/
    │   └── plugin_manager/          ← 플러그인 코드 (업데이트 시 교체)
    │       ├── plugin_manager.py
    │       ├── catalog.db           ← 구버전 레거시 위치
    │       └── plugin_sources.db    ← 구버전 레거시 위치
    └── data/
        └── plugin_manager/          ← 영속 데이터 (../../data/plugin_manager/)
            ├── plugin_manager.db    # 카탈로그·설정·소스 메타 통합 DB
            ├── catalog.db.bak       # 통합 마이그레이션 후 구 카탈로그 DB 백업
            ├── plugin_sources.db.bak # 통합 마이그레이션 후 구 소스 DB 백업
            └── .migrated            # 구버전 설정 마이그레이션 완료 플래그
```

### 저장되는 설정 키 (plugin_manager.db.settings)

| 키 | 설명 |
|-----|------|
| `PM_CATALOG_GITEA_SERVERS` | Gitea 서버 목록 (URL, 토큰, 활성화 상태) |
| `PM_CATALOG_TOPICS` | 카탈로그 검색 토픽 (쉼표 구분) |
| `PM_CATALOG_REFRESH_HOURS` | 카탈로그 갱신 간격 (1~24시간) |
| `PM_ALLOW_INVALID_INSTALL` | 검증 실패 플러그인 설치 허용 여부 |
| `PM_AUTO_UPDATE` | 플러그인 자동 업데이트 ON/OFF |
| `PM_UPDATE_PATH_SELECTION` | 플러그인별 업데이트 경로 선택 UI ON/OFF (기본 OFF, OFF 시 owner 기준 기본값 사용) |
| `PM_GITHUB_TOKEN` | GitHub API 토큰 (Bearer 인증용) |

### 특징

- **세션 독립적** — `general`/`adult`/`audiobook`/`video` 등 세션(db_type)과 무관하게 동일 설정 사용
- **업데이트 시 자동 마이그레이션** — 기존 `catalog.db`와 `plugin_sources.db`의 사용 중인 데이터를 `plugin_manager.db` 하나로 통합하고, 성공 후 원본 두 DB는 `.bak`으로 보존합니다. 아주 오래된 버전의 코어 DB 설정도 자체 DB `settings`로 1회 가져옵니다.
- **신규 설치 시** — `plugin_manager.db`가 자동 생성되며 별도 마이그레이션이 필요하지 않습니다.
- **토큰 보안** — Gitea/GitHub 토큰이 통합 DB에 평문 저장되므로 파일 권한 관리가 필요합니다. (Docker 볼륨 마운트 권장)

---

## Git 저장소 URL 설치

지원 URL 형식 (단독 플러그인 저장소 = 저장소 루트가 플러그인 자체):

```text
https://github.com/<owner>/<repo>[/tree/<branch>]     # GitHub (기본 브랜치 main)
https://github.com/<owner>/<repo>.git                  # .git 형식
https://<host>/<org>/<repo>[/src/branch/<branch>]      # Gitea 등 (archive/{branch}.zip 방식)
```

### 저장소 설치와 업데이트 ref

신규 Git 저장소 URL 설치는 URL에 지정된 브랜치(미지정 시 `main`) ZIP을 사용합니다. 설치 뒤 온라인 업데이트는 `branch` / `release` / `tag` 중 선택한 **하나의 ref**만 사용합니다.

- 저장소 URL에 브랜치가 명시되어 있으면 그 값을 `branch` 기준으로 저장합니다.
- **Gitea 저장소**도 GitHub와 동일하게 선택한 하나의 업데이트 경로만 사용합니다.
- 선택한 ref의 ZIP 다운로드가 실패해도 다른 업데이트 경로로 바꾸지 않습니다. 일반 플러그인의 레거시 raw 호환 전송 경로를 사용하더라도 같은 선택 ref를 유지합니다.
- 업데이트 완료 시 실제 사용한 소스가 표시됩니다 (`브랜치 main`, `릴리즈 v1.0.0`, `태그 v1.0.1`).

설치 절차:

1. 지정된 설치 브랜치의 소스 ZIP 다운로드 (표준 라이브러리만 사용, git 바이너리 불필요)
2. 저장소 루트에서 `update_manifest` 를 AST 로 추출 (없으면 설치 거부)
3. `update_manifest.files` 목록에 있는 파일만 남기고 **전부 삭제** (`.git`, `docs/`, 숨김 파일 포함)
4. `plugins/metadata/<plugin_id>` 로 복사 → 소스 메타 저장 → 활성화 + hot reload

설치 시 소스 메타가 `plugins/data/plugin_manager/plugin_manager.db`의 `plugin_sources` 테이블에 저장됩니다. 설치는 zip/git
어떤 방식이든 **`update_manifest.raw_base_url` 검증 기준**으로 판단합니다 — 유효한 GitHub 루트
주소면 `git_url` / `branch` / `update_channel` / `manifest_files` 이력이 남아 자동
업데이트·GitHub 배지가 활성화되고, manifest가 없거나 monorepo 서브디렉토리면 레코드가 없어
로컬 플러그인으로 유지됩니다 (이전 버전의 `.git_source`/`.zip_source` 파일은 설치 후 최초 1회
자동으로 DB에 마이그레이션됩니다 — `.zip_source`처럼 git_url이 없는 파일은 삭제 후
`update_manifest` 기준으로 재판단).
업데이트 확인과 실제 업데이트는 동일하게 해석한 `branch` / `release` / `tag` ref를 사용하므로 VERSION 확인 대상과 ZIP 설치 대상이 달라지지 않습니다.

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

### Plugin Manager 자기 업데이트

Plugin Manager도 일반 플러그인과 동일한 입력 경로를 지원합니다.

- **업데이트 버튼** — 저장소 ZIP을 받아 최신 버전으로 업데이트
- **ZIP 업로드** — `plugin_manager` 패키지면 신규 설치가 아니라 자기 업데이트로 처리
- **Git 저장소 URL** — 기존 코드 폴더를 삭제하지 않고 트랜잭션형 자기 업데이트로 처리
- **저장소 변경** — 카탈로그의 동일 플러그인 후보에 한해 변경 가능
- **자동/일괄 업데이트** — 일반 플러그인과 동일하게 대상에 포함
- **삭제** — 복구 UI 보호를 위해 계속 금지

자기 업데이트는 일반 플러그인보다 엄격하게 동작합니다. 현재 버전보다 높은 버전만 허용하고, raw fallback을 사용하지 않으며, 적용 후 `plugin_manager` provider 재로드와 VERSION 일치를 확인합니다. 실패하면 업데이트 전 코드 폴더로 자동 복원합니다.

### ZIP 우선 온라인 업데이트 정책

- 기존 설치 소스를 보존하고 저장소 ZIP을 먼저 받습니다.
- 최신 ZIP 내부의 `plugin_id`, `VERSION`, `update_manifest.files`를 최종 기준으로 사용합니다.
- 최신 manifest의 신규/삭제 파일을 반영하고, manifest 밖 런타임 데이터는 보존합니다.
- ZIP 패키지 검증 실패는 raw로 우회하지 않고 중단합니다.
- ZIP 자체 다운로드 실패에 한해서만 기존 raw 파일 업데이트를 호환용 fallback으로 사용합니다.
- 업데이트 후 로드 검증 실패 시 전체 백업에서 자동 복원합니다.


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

### 업데이트 경로 (자체 업데이트 엔진, 코어 PluginService 미사용)

기본값은 저장소 owner 기준으로 결정됩니다. owner가 **`madnite1`**이면 `release`, 그 외 저장소는 `branch`입니다. 설정의 **플러그인별 업데이트 경로 선택**을 켜면 설치된 업데이트 가능 플러그인 카드에 `branch / release / tag` 드롭다운이 표시되며, 사용자가 직접 선택한 값은 `plugin_manager.db`에 명시 선택으로 저장되어 기본값보다 우선합니다. 설정을 끄면 저장된 개별 선택값은 유지되지만 실제 업데이트는 owner 기준 기본값을 사용합니다.

- **branch** — 저장된 브랜치만 사용합니다.
- **release** — 최신 Release의 tag만 사용합니다. Release가 없거나 ref를 얻지 못하면 업데이트를 차단합니다.
- **tag** — 태그 목록에서 가장 높은 SemVer 태그만 사용합니다. 태그가 없으면 업데이트를 차단합니다.

세 경로 사이의 자동 폴백은 없습니다. 업데이트 확인용 `VERSION`, 원격 `update_manifest`, 실제 다운로드 ZIP은 모두 같은 선택 ref를 기준으로 처리합니다.

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
`release` 업데이트 경로를 선택한 플러그인에서 감지됩니다.

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

### 업데이트 후 사용자 롤백

- 성공한 플러그인 업데이트의 직전 코드와 `plugins/data/<plugin_id>` 영속 데이터를 1개 롤백 슬롯에 함께 보관합니다.
- SQLite 데이터 파일은 가능한 경우 SQLite backup API로 일관된 스냅샷을 만들고, 일반 파일과 심볼릭 링크도 함께 보존합니다.
- 플러그인 카드의 **롤백** 버튼으로 코드와 영속 데이터를 모두 업데이트 직전 시점으로 되돌릴 수 있습니다. 업데이트 이후 생성·변경된 데이터는 롤백 시 이전 값으로 돌아갑니다.
- 업데이트 후 로드 검증에 실패하면 사용자 롤백을 기다리지 않고 코드와 영속 데이터를 모두 자동 복구합니다.
- `update_manifest.files` 밖의 코드 폴더 런타임 파일은 기존 정책대로 현재 값을 유지합니다.
- `plugin_manager` 자기 롤백은 자기 데이터 폴더 안의 `rollback/` 저장소를 스냅샷에서 제외하고 복원 중에도 보존해 재귀 백업을 방지합니다.
- 롤백은 업데이트 때 생성된 직전 상태 백업을 **1회 소비**합니다. 롤백 성공 후 백업 슬롯을 삭제하므로 방금 사용하던 업데이트 버전은 롤백 대상으로 남지 않으며, 다시 업데이트하기 전까지 추가 롤백은 제공하지 않습니다.
- 플러그인을 삭제하면 해당 플러그인의 롤백 슬롯도 항상 함께 삭제합니다. 영속 데이터 폴더는 삭제 모달에서 별도로 선택한 경우에만 제거합니다.
- 데이터 폴더가 큰 플러그인은 업데이트 전에 전체 스냅샷을 만들기 때문에 시간이 더 걸리고 디스크 사용량이 증가할 수 있습니다. 롤백 슬롯은 플러그인별로 직전 1개만 유지합니다.
