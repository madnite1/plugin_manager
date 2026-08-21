---
name: deploy-develop-version
description: >-
  Use this skill when the user asks to run "북오아시스 플러그인 개발배포" or deploy the
  plugin_manager plugin files to the running BookOasis server. This skill reads plugin_id
  and update_manifest.files from the plugin source code in the current project, then copies
  those files to /home/ubuntu/BookOasis/plugins/metadata/<plugin_id>, then optionally
  restarts the BookOasis Docker container.
  When the user says "북오아시스 플러그인 개발배포 원복", restore files from DST/bak to DST and delete bak.
  Only activate this skill for BookOasis plugin_manager deployment tasks specifically.
---

# 개발배포 (deploy-develop-version)

현재 작업 중인 플러그인 개발 저장소의 파일을 운영 중인 BookOasis 서버의 플러그인 폴더로 복사하는 배포 절차입니다.

## 경로 결정 규칙

- **개발 소스 (`SRC`)**: 현재 작업 중인 프로젝트 루트 디렉토리 (workspace 루트)
- **plugin_id**: 프로젝트 소스 코드(메인 `.py` 파일)에 선언된 `BaseMetadataProvider` 서브클래스의 `id` 속성값
- **배포 대상 (`DST`)**: `/home/ubuntu/BookOasis/plugins/metadata/<plugin_id>`
- **Docker 컨테이너**: `bookoasis`

## 배포 대상 파일

프로젝트 소스 코드에 선언된 `update_manifest.files` 목록을 읽어 복사할 파일을 결정한다.
`update_manifest`는 메인 Python 파일 내 클래스 속성으로 선언되어 있으므로,
파일을 직접 열어 `"files"` 키의 값을 확인한다.

## 배포 절차

### 1. plugin_id 및 파일 목록 확인

프로젝트 소스 코드를 직접 열어 클래스의 `id` 속성과 `update_manifest["files"]` 를 확인한다.
grep으로 빠르게 찾을 수도 있다:

```bash
grep -rE '^\s+id\s*=|"files"\s*:' <SRC>/*.py
```

### 2. 기존 파일 백업

복사 전, 배포 대상(`DST`)에 있는 `update_manifest.files` 파일들을 `DST/bak` 폴더에 백업한다.
**`DST/bak` 안에 파일이 하나라도 존재하면 백업을 건너뛴다.**

```bash
BAK="$DST/bak"
# bak 폴더에 파일이 없을 때만 백업 실행
if [ -z "$(ls -A "$BAK" 2>/dev/null)" ]; then
    mkdir -p "$BAK"
    for f in <update_manifest.files 목록>; do
        [ -f "$DST/$f" ] && cp -v "$DST/$f" "$BAK/$f"
    done
    echo "백업 완료: $BAK"
else
    echo "bak 폴더에 파일이 존재하여 백업을 건너뜁니다: $BAK"
fi
```

### 3. 파일 복사

확인한 `SRC`, `DST`, 파일 목록을 사용해 복사한다:

```bash
SRC="<현재_프로젝트_루트>"
DST="/home/ubuntu/BookOasis/plugins/metadata/<plugin_id>"

for f in <update_manifest.files 목록>; do
    cp -v "$SRC/$f" "$DST/$f"
done
```

복사 결과(각 파일의 `→` 출력)를 확인해 모든 파일이 정상 복사됐는지 검증한다.

### 4. Docker 재시작 (선택)

사용자가 서버 재시작을 함께 요청한 경우에만 실행한다:

```bash
sudo docker restart bookoasis
```

재시작 후 `sudo docker ps | grep bookoasis` 로 `Up N seconds` 상태를 확인한다.

## 원복 절차

사용자가 **"북오아시스 플러그인 개발배포 원복"** 을 요청한 경우 아래 절차를 수행한다.

### 1. plugin_id 확인

개발 소스 또는 배포 대상 경로에서 `plugin_id` 를 확인한다 (배포 절차 1단계와 동일).

### 2. bak 파일을 대상 폴더로 복사

```bash
DST="/home/ubuntu/BookOasis/plugins/metadata/<plugin_id>"
BAK="$DST/bak"

if [ -d "$BAK" ] && [ -n "$(ls -A "$BAK" 2>/dev/null)" ]; then
    for f in "$BAK"/*; do
        cp -v "$f" "$DST/$(basename "$f")"
    done
    echo "원복 완료"
else
    echo "bak 폴더가 비어있거나 존재하지 않습니다: $BAK"
fi
```

### 3. bak 폴더 삭제

```bash
rm -rf "$BAK"
echo "bak 폴더 삭제 완료"
```

### 4. Docker 재시작 (선택)

사용자가 요청한 경우에만 실행한다:

```bash
sudo docker restart bookoasis
```

---

## 주의사항

- `update_manifest.files` 목록에 없는 파일(예: `catalog.db`, `plugin_sources.db` 등 데이터 파일)은 복사하지 않는다 (영속 데이터 보호).
- Docker 재시작 없이도 BookOasis hot reload 기능으로 플러그인이 반영될 수 있다.
