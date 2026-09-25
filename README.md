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

- **ZIP 업로드 설치** — 플러그인 전체 폴더가 들어 있는 ZIP을 업로드합니다. `update_manifest`는 필수가 아닙니다.
  - 같은 `plugin_id`가 이미 설치되어 있으면 신규 설치가 아니라 **전체 폴더 교체 업데이트**로 처리합니다.
  - `plugins/metadata/<plugin_id>`는 새 패키지 기준으로 통째로 교체하고, `plugins/data/<plugin_id>`와 `plugins/cache/<plugin_id>`는 보존합니다.
  - 업데이트 전에 기존 코드와 영속 데이터를 백업하고, 핫 리로드/사후 검증 실패 시 기존 상태로 자동 복원합니다.
  - 기존 저장소 소스 정보가 있는 플러그인은 ZIP 업데이트만으로 업데이트 원본 저장소가 바뀌지 않습니다.

설치 후 온라인 업데이트는 저장된 저장소 URL과 `branch` / `release` / `tag` 전략을 기준으로 **HTTP ZIP을 다운로드**합니다. `git clone`이나 Git 바이너리는 사용하지 않습니다. ZIP 내부의 `plugin_id`와 `VERSION`을 검증한 뒤 `plugins/metadata/<plugin_id>` 폴더 전체를 새 패키지로 교체합니다. 별도 설치 소스가 없는 오래된 설치본에 한해 `update_manifest`를 레거시 raw fallback 힌트로만 사용할 수 있습니다.

### BookOasis 1.1.0+ 선택 계약 검증

Plugin Manager는 Provider를 실행하지 않고 AST로 `home_widget`, `detail_sidebar_widget`, `detail_view` 선언을 분석합니다. 리터럴 선언은 공식 문서 allowlist에 맞춰 검증하고, 일부 값이 변수/함수 호출인 동적 표현식이면 해당 필드만 `판정 불가`로 격리해 설치를 불필요하게 차단하지 않습니다.

- `sessions`: 생략, `"all"`, 또는 `general/adult/audiobook/video` 값의 문자열 리스트를 지원합니다.
- `home_widget`: `layout`은 `full|grid`, `size`는 `1|2|3`을 검사합니다. `get_dashboard_data()` 직접 구현을 정적으로 확인하지 못하면 경고만 표시합니다.
- `detail_sidebar_widget`: `get_detail_sidebar_data()` 직접 구현을 정적으로 확인하지 못하면 경고만 표시합니다.
- `detail_view`: `detail/index.html`, `detail/style.css`, `detail/script.js` 3개 파일이 필요합니다. 업데이트는 플러그인 폴더 전체를 교체하므로 `update_manifest.files`에 별도 등록할 필요가 없습니다.
- 설치 후 `_verify_installed_plugin_static()`은 기존처럼 Provider id와 선택적 VERSION의 최소 무결성만 확인합니다. 신규 UI 계약 검증을 사후 삭제/롤백 게이트로 사용하지 않아 `force` 설치의 기존 의미를 유지합니다.
- 설치된 플러그인 카드에는 **실제로 지원하는 홈 / 상세 사이드바 / 상세 뷰만** 기존 **카테고리 뷰**와 같은 기능 배지로 표시합니다. 미지원 또는 정적으로 판정할 수 없는 항목은 카드에 별도 배지를 만들지 않으며, 미설치 카탈로그 항목은 원격 소스를 추가 분석하지 않습니다.

### 1.14.48 커스텀 설정값 표시 수정

플러그인 매니저에서 커스텀 `settings.html`을 여는 경우 코어의 **설정 > 플러그인 설정** 화면과 동일하게 저장된 config 값을 `name` 기반 입력 필드에 먼저 주입한 뒤 플러그인의 `settings.js`를 실행하도록 수정했습니다. 따라서 Rabbit 플러그인의 YES24 Open API 키처럼 코어 설정 화면에서는 보이지만 플러그인 매니저 설정 모달에서는 비어 보이던 값이 정상 표시됩니다. 체크박스와 일반 입력 필드를 모두 처리합니다.

### 1.14.47 플러그인 문서 뷰어

플러그인 매니저 설정에 **문서 목록**을 추가했습니다. 기본값은 빈 목록이며 `README.md`, `CHANGES.md`처럼 플러그인 폴더 기준의 Markdown(`.md`) 파일명만 등록할 수 있습니다. 설정된 파일 중 각 설치 플러그인 폴더에 실제로 존재하는 문서가 있을 때만 카드에 **문서** 버튼이 표시됩니다. 버튼을 누르면 전용 Markdown 모달에서 등록된 문서를 선택해 읽을 수 있습니다. 문서 목록이 비어 있거나 해당 플러그인에 등록 문서가 하나도 없으면 버튼은 표시되지 않습니다. 경로 이탈(`..`), 절대경로, 심볼릭 링크는 차단하며 UTF-8 Markdown만 읽습니다. 각 플러그인 Provider 소스에 별도 문서 선언을 추가할 필요는 없습니다.

### 1.14.45 Gitea 카탈로그 토픽 검색 엄격화

Gitea 카탈로그 수집에서 저장소명/설명을 대상으로 하던 `q=` 키워드 검색 폴백을 제거했습니다. 이제 설정한 토픽이 저장소의 실제 `topics`에 포함된 경우에만 카탈로그 후보로 수집되며, 서버가 토픽 검색 파라미터를 무시하더라도 클라이언트에서 토픽을 다시 검사해 원치 않는 저장소를 제외합니다. 또한 Gitea 서버의 모든 토픽 검색이 정상 완료된 경우 이번 검색 결과에 없는 미설치 저장소는 기존 카탈로그 DB에서도 정리합니다. 서버 조회 실패 시에는 기존 항목을 보존해 일시적인 장애로 카탈로그가 사라지지 않도록 했습니다.

### 1.14.44 저장소 변경 모달 종료 처리

저장소 변경 모달에서 `변경하기`를 누르면 교체 요청의 완료 응답을 기다리지 않고 선택 모달을 즉시 닫습니다. 다운로드·재설치·의존성 처리 중에는 진행 알림을 표시하고, 완료 후 성공/실패 결과를 별도 알림으로 안내합니다. 네트워크 응답이 지연되거나 끊겨도 교체 모달이 화면에 남아 있는 문제를 방지합니다.

### 1.14.37 목록 필터와 검색 UI 정리

플러그인 목록의 상태/기능 필터 의미를 정리했습니다. `활성화`와 `비활성화`는 설치된 플러그인만 대상으로 필터링하고 카운트도 설치본만 집계합니다. 상태 필터는 전체/설치됨/미설치/활성화/비활성화로 정리하고, 기능 필터에는 카테고리 뷰·대시보드 위젯뿐 아니라 상세 뷰·상세 사이드바·홈·수동 검색을 추가했습니다. 기능 필터는 설치본의 정적 Provider 계약을 기준으로 동작합니다. 검색 입력은 필터와 별도 행의 전체 너비로 확장해 좁은 화면에서도 충분한 입력 공간을 확보했습니다.

### 1.14.36 위험 설치 후 검증 상태 유지 표시

카탈로그에서 `invalid` 또는 `unknown`인 저장소를 위험 설치한 뒤에도 현재 설치 소스의 카탈로그 검증 상태를 설치 카드에 유지합니다. 현재 설치된 `git_url`과 정확히 일치하는 카탈로그 행만 상태 판정에 사용하므로 같은 plugin_id의 다른 저장소 상태가 섞이지 않습니다. 검증 실패는 `검증 실패`, 검증 대기는 `검증 대기`로 표시하고, 카탈로그가 정상/미등록이더라도 Git 설치본에 활성 `update_manifest`가 없으면 `업데이트 미지원`을 표시합니다. 플러그인 실행/사용은 그대로 허용하며 경고 상태만 유지합니다.

### 1.14.35 저장소 기본 브랜치 판정 수정

저장된 Git 소스 URL은 계속 업데이트 원본 저장소의 기준으로 사용하되, 브랜치는 저장 DB 값을 무조건 신뢰하지 않습니다. `URL에 명시된 branch → 카탈로그/저장소 API의 실제 default_branch → 같은 저장소의 update_manifest branch → 저장된 branch → main` 순서로 결정합니다. 따라서 과거에 브랜치 없는 Git URL을 설치하면서 `main`이 자동 저장됐더라도 실제 기본 브랜치가 `master`인 저장소는 정상적으로 업데이트를 확인합니다. Git 설치/업데이트 ZIP 다운로드도 같은 판정 로직을 사용해 신규 설치부터 실제 기본 브랜치를 저장하며, 서로 다른 저장소를 가리키는 오래된 `update_manifest.raw_base_url`은 계속 무시해 1.14.34의 저장소 정합성 수정도 유지합니다.

### 1.14.34 카탈로그 검증 상태 표시와 설치 소스 정합성

카탈로그 재검증에서 `invalid` 또는 `unknown`으로 판정된 미설치 저장소를 목록에서 숨기지 않고 `검증 실패`/`검증 대기` 상태로 표시합니다. 검증 실패 설치 허용 설정이 꺼져 있으면 설치 버튼만 차단하고, 켜져 있으면 기존 검증 실패 확인 모달을 거쳐 위험 설치를 선택할 수 있습니다. 저장소 변경 후보는 안전을 위해 기존처럼 `valid` 저장소만 사용합니다. 또한 설치된 플러그인의 `plugin_sources.git_url`/branch를 온라인 업데이트의 최종 원본으로 사용해, 플러그인 코드에 남은 과거 `update_manifest.raw_base_url`이 다른 저장소를 가리키더라도 잘못된 저장소의 VERSION을 조회하지 않습니다.

### 1.14.33 저장소 변경 후보 실시간 갱신

저장소 변경 모달을 열 때 화면 최초 로드 시점의 `replace_candidates`를 그대로 재사용하지 않고 백엔드에서 최신 카탈로그 후보를 다시 조회합니다. `check_update` 응답은 업데이트 차단 여부와 관계없이 현재 저장소 변경 후보 배열을 항상 포함하며, 비동기 업데이트 확인에서도 카드의 후보와 저장소 변경 버튼을 최신 상태로 동기화합니다. 따라서 카탈로그 DB가 새 버전으로 갱신된 뒤에도 이미 열린 플러그인 매니저 화면의 저장소 변경 모달이 이전 버전을 계속 표시하던 문제를 수정했습니다.

### 1.14.32 카탈로그 변경 저장소 즉시 재검증

저장소가 20개를 넘어 24시간 VERSION 검증 캐시를 사용하는 경우에도, 검색 API의 `pushed_at`/`updated_at`이 `last_checked`보다 최신이면 해당 저장소를 즉시 재검증합니다. 따라서 저장소에 새 버전이 push된 뒤에도 카탈로그 카드와 저장소 변경 후보가 이전 버전으로 최대 24시간 남아 있던 문제를 수정했습니다. 변경되지 않은 저장소만 기존 TTL 캐시를 재사용합니다.

### 1.14.31 카탈로그/저장소 변경 검증 정합성 수정

카탈로그의 `valid` 판정에서도 Provider의 활성 `update_manifest` 최소 계약을 확인합니다. `update_manifest`가 없거나 비활성/비정상인 저장소는 저장소 변경 후보에서는 제외되며, 저장소 변경은 `force` 여부와 관계없이 유효한 `update_manifest.files`를 필수로 요구합니다. 직접 Git 설치의 `검증 실패시 설치 가능` 옵션은 기존대로 유지합니다. 1.14.34부터는 이런 저장소도 일반 미설치 카탈로그 목록에는 검증 상태와 함께 표시됩니다. 또한 수동 카탈로그 갱신은 24시간 검증 캐시를 무시하고 전체 저장소를 즉시 재검증해 오래된 `valid` 판정이 남지 않도록 했습니다.

### 1.14.30 중첩 검증 모달 표시 수정

저장소 변경처럼 다른 모달이 열린 상태에서 플러그인 검증 실패 확인이 필요한 경우, 검증 모달을 전용 최상위 레이어로 표시합니다. 동적 저장소 변경 모달이 검증 모달을 가려 진행이 멈춘 것처럼 보이던 문제를 수정했습니다.

### 1.14.29 소스 표시 단순화

1.14.29부터 플러그인 카드와 저장소 변경 UI에서 GitHub/Gitea를 별도 배지로 구분하지 않고 **`GIT` / `LOCAL`** 두 종류만 표시합니다. 저장소 URL, 인증 토큰, 카탈로그 수집, release/tag/branch 처리에는 기존 GitHub/Gitea 구분을 그대로 유지하므로 동작에는 영향이 없습니다.

### 1.14.27 저장소 이전 처리 수정

1.14.27부터 카탈로그에 동일 `plugin_id`의 다른 저장소가 발견되면 현재 업데이트가 차단된 상태가 아니어도 **저장소 변경** 버튼을 표시합니다. 따라서 기존 플러그인의 `update_manifest.enabled`가 `False`이거나 현재 업데이트 확인을 수행하지 않는 상태에서도 GitHub → Gitea 같은 저장소 이전 후보를 선택할 수 있습니다.

### 1.14.26 UI 정리

1.14.26부터 BookOasis 1.1.0+ 선택 계약 표시는 별도의 `지원` 행과 상태 아이콘을 만들지 않고, 기존 `카테고리 뷰` / `대시보드 위젯`과 동일한 기능 배지 영역에 합칩니다. 지원하는 기능만 표시하므로 카드가 불필요하게 길어지거나 `?` 배지가 반복되지 않습니다.

### 1.14.25 배포 검증

1.14.25는 BookOasis 1.1.0+ 선택 계약 대응과 플러그인 카드 지원 여부 표시를 포함합니다. 배포 전 다음 항목을 확인했습니다.

- 회귀 테스트 `tests/test_plugin_manager_contracts.py` 14개 전부 통과
- Python 문법 검사 및 `script.js` 문법 검사 통과
- 일반/엄격 검증과 `update_manifest`, 릴리즈 준비 검사 통과
- 기존 설치 플러그인 호환성 회귀 검사를 수행했으며, 기존 통과/실패 판정 및 실패 사유에 신규 회귀가 없음을 확인
- 1.14.24 설치 상태에서 `update_manifest.files` 8개만 포함한 1.14.25 ZIP 업데이트 성공 확인

1.14.25 배포 ZIP은 아래 8개 파일만 포함해야 합니다. README와 테스트 파일은 배포 ZIP 대상이 아닙니다.

```text
plugin_manager.py
__init__.py
VERSION
index.html
style.css
script.js
settings.html
settings.js
```

---

## 데이터 영속성 아키텍처

BookOasis 플러그인은 코드/메타데이터, 영속 데이터, 재생성 가능한 캐시를 각각 분리합니다. 공통 경로는 `plugins/metadata/<plugin_id>`, `plugins/data/<plugin_id>`, `plugins/cache/<plugin_id>`입니다. 업데이트·재설치 시 영속 데이터와 캐시는 코드 폴더와 분리되며, 플러그인 삭제 시 `plugins/cache/<plugin_id>`는 항상 함께 삭제됩니다. `plugins/data/<plugin_id>`가 존재하는 경우에만 삭제 모달에서 영속 데이터까지 함께 지울지 선택할 수 있으며, 선택하지 않으면 보존됩니다.

### 디렉토리 구조

```text
BookOasis/
└── plugins/
    ├── metadata/
    │   └── plugin_manager/          ← 플러그인 코드 (업데이트 시 교체)
    │       ├── plugin_manager.py
    │       ├── catalog.db           ← 구버전 레거시 위치
    │       └── plugin_sources.db    ← 구버전 레거시 위치
    ├── data/
    │   └── plugin_manager/          ← 영속 데이터 (../../data/plugin_manager/)
    │       ├── plugin_manager.db    # 카탈로그·설정·소스 메타 통합 DB
    │       ├── catalog.db.bak       # 통합 마이그레이션 후 구 카탈로그 DB 백업
    │       ├── plugin_sources.db.bak # 통합 마이그레이션 후 구 소스 DB 백업
    │       └── .migrated            # 구버전 설정 마이그레이션 완료 플래그
    └── cache/
        └── plugin_manager/          ← 재생성 가능한 캐시 영역
```

### 저장되는 설정 키 (plugin_manager.db.settings)

| 키 | 설명 |
|-----|------|
| `PM_CATALOG_GITEA_SERVERS` | Gitea 서버 목록 (URL, 토큰, 활성화 상태) |
| `PM_CATALOG_TOPICS` | 카탈로그 검색 토픽 (쉼표 구분) |
| `PM_CATALOG_REFRESH_HOURS` | 카탈로그 갱신 간격 (1~24시간) |
| `PM_DOCUMENT_FILES` | 플러그인 카드 문서 뷰어에서 확인할 Markdown 파일 목록 (기본 빈 목록) |
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

신규 Git 저장소 URL 설치는 URL에 브랜치가 명시되면 해당 브랜치를 사용하고, 미지정이면 카탈로그/저장소 API의 실제 기본 브랜치를 확인한 뒤 ZIP을 사용합니다. 기본 브랜치를 확인할 수 없는 경우에만 `main`을 최종 폴백으로 사용합니다. 설치 뒤 온라인 업데이트는 `branch` / `release` / `tag` 중 선택한 **하나의 ref**만 사용합니다.

- 저장소 URL에 브랜치가 명시되어 있으면 그 값을 `branch` 기준으로 저장합니다.
- **Gitea 저장소**도 GitHub와 동일하게 선택한 하나의 업데이트 경로만 사용합니다.
- 선택한 ref의 ZIP 다운로드가 실패해도 다른 업데이트 경로로 바꾸지 않습니다. 일반 플러그인의 레거시 raw 호환 전송 경로를 사용하더라도 같은 선택 ref를 유지합니다.
- 업데이트 완료 시 실제 사용한 소스가 표시됩니다 (`브랜치 main`, `릴리즈 v1.0.0`, `태그 v1.0.1`).

설치 절차:

1. 지정된 저장소 ref의 소스 ZIP을 HTTP로 다운로드합니다. Git 바이너리나 `git clone`은 사용하지 않습니다.
2. ZIP을 안전하게 해제하고 Provider의 `plugin_id`와 기본 플러그인 계약을 정적으로 검증합니다.
3. 신규 설치는 플러그인 전체 폴더를 `plugins/metadata/<plugin_id>`로 복사합니다.
4. 기존 설치본이면 새 폴더를 staging한 뒤 기존 `plugins/metadata/<plugin_id>` 전체와 교체하고 hot reload/사후 검증을 수행합니다.
5. `plugins/data/<plugin_id>`와 `plugins/cache/<plugin_id>`는 코드 폴더 밖에 있으므로 업데이트에서 보존됩니다.

설치 시 소스 메타가 `plugins/data/plugin_manager/plugin_manager.db`의 `plugin_sources` 테이블에 저장됩니다. 저장소 소스가 저장된 뒤에는 **`plugin_sources.git_url` / `branch` / `update_channel`을 온라인 업데이트 원본의 최종 기준**으로 사용합니다. `update_manifest`는 일반 설치·업데이트의 필수 조건이 아니며, 별도 소스 메타가 없는 레거시 설치본에서만 선택적인 fallback 힌트로 사용합니다. 저장소 소스가 있으면 자동 업데이트와 저장소 기반 상태가 활성화되고, 소스 메타가 없으면 **LOCAL** 플러그인으로 표시됩니다. 이전 버전의 `.git_source`/`.zip_source` 파일은 설치 후 최초 1회 DB로 마이그레이션됩니다.
업데이트 확인과 실제 업데이트는 동일하게 해석한 `branch` / `release` / `tag` ref를 사용하므로 VERSION 확인 대상과 ZIP 설치 대상이 달라지지 않습니다.

---

## 저장소 변경 (Replace Source)

설치된 플러그인과 동일한 `plugin_id`를 가진 다른 카탈로그 저장소가 발견되면, 현재 원격 저장소의 업데이트 가능 여부와 관계없이 후보 저장소(GitHub/Gitea)로 **저장소를 변경**할 수 있습니다. 원격 저장소 삭제/이동/접근 불가로 업데이트가 차단된 경우에는 기존 차단 상태 배지도 함께 표시합니다.

### 동작 조건
- 카탈로그에 동일 플러그인 ID의 다른 소스(GitHub 또는 활성화된 Gitea) 후보가 있어야 함
- 현재 설치 소스와 URL이 같은 카탈로그 항목은 후보에서 제외
- `update_manifest.enabled=False`처럼 온라인 업데이트가 비활성화된 설치본도 저장소 변경 후보는 독립적으로 표시
- 시스템 플러그인(`plugin_manager`)은 제외

### 사용 방법
1. 동일 플러그인의 다른 저장소가 발견되면 플러그인 카드에 **"저장소 변경"** 버튼이 표시됨
2. 현재 저장소가 실제로 업데이트 불가 상태면 원인별 차단 배지도 함께 표시됨
3. 버튼 클릭 → 모달에서 후보 저장소 선택 (버전, 소스 타입, URL 확인 가능)
4. **"변경하기"** 클릭 → 기존 플러그인 백업 → 선택한 소스에서 재설치 → 성공 시 완료, 실패 시 자동 롤백

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

업데이트/소스 교체/롤백 중 사용하는 임시 작업 폴더는 `plugins/data/plugin_manager/work/` 아래에 격리하여 `plugins/metadata` 플러그인 탐색 대상에 노출되지 않게 합니다. 또한 구버전이 `plugins/metadata` 루트에 남긴 `.pm_*` 임시 폴더는 자기 업데이트 성공 시뿐 아니라 `start_background_service()` 실행 시에도 정리하므로, 구버전 코드가 업데이트 요청을 끝까지 처리해 즉시 정리하지 못한 경우에도 새 버전 로드 또는 BookOasis 재기동 시 자동 제거됩니다.

### HTTP ZIP 전체 폴더 업데이트 정책

- 기존 설치 소스를 보존하고 선택한 `branch` / `release` / `tag`의 저장소 ZIP을 HTTP로 받습니다.
- 최신 ZIP 내부의 `plugin_id`와 `VERSION`을 최종 기준으로 사용합니다.
- 새 패키지의 플러그인 폴더 전체를 staging 검증한 뒤 `plugins/metadata/<plugin_id>` 전체와 교체합니다.
- 코드 폴더 안에서 새 버전에 사라진 파일은 자연스럽게 제거되며 별도 파일 목록 관리가 필요하지 않습니다.
- `plugins/data/<plugin_id>`와 `plugins/cache/<plugin_id>`는 업데이트에서 보존합니다.
- ZIP 다운로드 또는 패키지 검증 실패는 다른 업데이트 경로로 자동 폴백하지 않고 중단합니다.
- 업데이트 후 hot reload/사후 검증 실패 시 코드와 영속 데이터 백업에서 자동 복원합니다.

`update_manifest`는 선택적인 레거시/self-update 힌트로만 남아 있으며 Plugin Manager의 일반 저장소 설치·업데이트에는 필요하지 않습니다. Plugin Manager 자기 업데이트도 저장된 저장소 소스 + `VERSION` + 전체 패키지 검증으로 처리합니다.

### 업데이트 경로 (자체 업데이트 엔진, 코어 PluginService 미사용)

기본값은 저장소 owner 기준으로 결정됩니다. owner가 **`madnite1`**이면 `release`, 그 외 저장소는 `branch`입니다. 설정의 **플러그인별 업데이트 경로 선택**을 켜면 설치된 업데이트 가능 플러그인 카드에 `branch / release / tag` 드롭다운이 표시되며, 사용자가 직접 선택한 값은 `plugin_manager.db`에 명시 선택으로 저장되어 기본값보다 우선합니다. 설정을 끄면 저장된 개별 선택값은 유지되지만 실제 업데이트는 owner 기준 기본값을 사용합니다.

- **branch** — 저장된 브랜치만 사용합니다.
- **release** — 최신 Release의 tag만 사용합니다. Release가 없거나 ref를 얻지 못하면 업데이트를 차단합니다.
- **tag** — 태그 목록에서 가장 높은 SemVer 태그만 사용합니다. 태그가 없으면 업데이트를 차단합니다.

세 경로 사이의 자동 폴백은 없습니다. 업데이트 확인용 `VERSION`과 실제 다운로드 ZIP은 모두 같은 선택 ref를 기준으로 처리합니다.

저장된 저장소 소스와 정상 `VERSION`이 있으면 `update_manifest` 없이도 업데이트 대상이 됩니다. 버전 비교는 SemVer core(`MAJOR.MINOR.PATCH`)만 사용하고 `v` 접두사·pre-release 접미사는 무시하며, 로컬 < 원격일 때만 업데이트를 허용합니다.

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
- 업데이트/소스 교체/롤백용 임시 코드 작업본은 `plugins/data/plugin_manager/work/` 아래에 두어 `plugins/metadata` 플러그인 탐색 대상과 분리합니다.
- `plugin_manager` 자기 업데이트 성공 시와 이후 백그라운드 서비스 시작 시 구버전이 `plugins/metadata` 루트에 남긴 `.pm_*` 임시 작업 잔재를 자동 삭제합니다.
- `plugins/metadata/<plugin_id>`는 업데이트 시 새 패키지로 전체 교체되므로 코드 폴더 안의 구버전 전용 파일은 자동으로 제거됩니다. 영속 상태는 반드시 `plugins/data/<plugin_id>`에 둡니다.
- `plugin_manager` 자기 롤백은 자기 데이터 폴더 안의 `rollback/` 저장소를 스냅샷에서 제외하고 복원 중에도 보존해 재귀 백업을 방지합니다.
- 롤백은 업데이트 때 생성된 직전 상태 백업을 **1회 소비**합니다. 롤백 성공 후 백업 슬롯을 삭제하므로 방금 사용하던 업데이트 버전은 롤백 대상으로 남지 않으며, 다시 업데이트하기 전까지 추가 롤백은 제공하지 않습니다.
- 플러그인을 삭제하면 해당 플러그인의 롤백 슬롯과 `plugins/cache/<plugin_id>` 캐시 폴더를 항상 함께 삭제합니다. 영속 데이터 폴더는 삭제 모달에서 별도로 선택한 경우에만 제거합니다.
- 데이터 폴더가 큰 플러그인은 업데이트 전에 전체 스냅샷을 만들기 때문에 시간이 더 걸리고 디스크 사용량이 증가할 수 있습니다. 롤백 슬롯은 플러그인별로 직전 1개만 유지합니다.
