// plugins/metadata/plugin_manager/settings.js
// 코어 openSettingsModal이 new Function('window','pluginId','root','config')로 실행
// 저장은 공용 모달 하단 "설정 저장" 버튼 사용 (script.js saveSettingsModal → plugin_manager 분기)
// 여기서는 초기값 로드 + 코어 폼 submit 가로채기(자체 save-config) 담당
//
// 왜 가로채는가: 코어 plugins.js의 .plugin-config-form submit 핸들러는 name 속성이 있는
// input만 취합해 PLUGIN_CONFIG_plugin_manager 키에 저장한다. plugin_manager는 PM_CATALOG_*
// 키에서 읽으므로 코어 경로로는 저장값이 반영되지 않는다. 따라서 capture 단계에서 submit을
// 가로채(코어 핸들러는 bubble 단계라 먼저 실행됨) 자체 save-config API를 호출한다 —
// 카탈로그 톱니 버튼(script.js saveCatalogSettings)과 동일한 경로/페이로드/저장 키.
(function(window, pluginId, root, config) {
    'use strict';

    if (!root) {
        root = document.querySelector('[data-plugin-settings-root="plugin_manager"]');
    }
    if (!root) return;

    const intervalInput = root.querySelector('#pm-catalog-interval');
    const topicsInput = root.querySelector('#pm-catalog-topics');
    const allowInvalidInput = root.querySelector('#pm-allow-invalid-install');
    const autoUpdateInput = root.querySelector('#pm-auto-update');
    const tokenInput = root.querySelector('#pm-github-token');
    const tokenClearBtn = root.querySelector('#pm-token-clear');
    const giteaUrlInput = root.querySelector('#pm-gitea-url');
    const giteaTokenInput = root.querySelector('#pm-gitea-token');
    const giteaAddBtn = root.querySelector('#pm-gitea-add');
    const giteaListEl = root.querySelector('#pm-gitea-list');

    // Gitea 서버 로컬 목록 (마스킹 토큰 포함 — 저장 시 백엔드가 마스킹이면 기존 유지)
    let giteaServers = [];

    // script.js saveCatalogSettings(실제 저장 경로)에서 접근할 수 있도록 window에 노출
    window.__pm_gitea_servers_get = function() { return giteaServers; };

    function renderGiteaList() {
        if (!giteaListEl) return;
        giteaListEl.innerHTML = '';
        if (!giteaServers.length) {
            const empty = document.createElement('div');
            empty.style.cssText = 'font-size: 0.8rem; color: var(--app-text-muted, #94a3b8); padding: 0.4rem 0;';
            empty.textContent = '등록된 Gitea 서버가 없습니다.';
            giteaListEl.appendChild(empty);
            return;
        }
        giteaServers.forEach(function(s, idx) {
            const row = document.createElement('div');
            row.style.cssText = 'display: flex; align-items: center; gap: 0.6rem; background: rgba(255,255,255,0.04); border: 1px solid var(--app-border, rgba(255,255,255,0.12)); border-radius: 6px; padding: 0.45rem 0.7rem;';
            const urlSpan = document.createElement('span');
            urlSpan.style.cssText = 'flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.82rem; color: var(--app-text-primary, #fff);';
            urlSpan.textContent = s.url;
            const tokSpan = document.createElement('span');
            tokSpan.style.cssText = 'font-size: 0.74rem; color: var(--app-text-muted, #94a3b8); white-space: nowrap;';
            tokSpan.textContent = s.token ? ('토큰: ' + s.token) : '토큰 없음';
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.innerHTML = '<i class="fa-solid fa-trash"></i>';
            delBtn.title = '서버 삭제';
            delBtn.style.cssText = 'cursor: pointer; border: none; background: transparent; color: var(--app-text-muted, #94a3b8); font-size: 0.85rem; padding: 0.2rem 0.4rem; border-radius: 4px;';
            delBtn.addEventListener('click', function() {
                giteaServers.splice(idx, 1);
                renderGiteaList();
            });
            row.appendChild(urlSpan);
            row.appendChild(tokSpan);
            row.appendChild(delBtn);
            giteaListEl.appendChild(row);
        });
    }

    // 초기값 로드 — /data 응답의 catalog_meta (간격/토픽은 MariaDB 설정)
    // 레이스 방지: 이미 값이 변경된(사용자가 입력한) 필드는 덮어쓰지 않음
    async function loadInitial() {
        try {
            const res = await fetch('/api/media/dashboard/widgets/plugin_manager/data?type=general');
            const data = await res.json();
            if (!data.success || !data.catalog_meta) return;
            const meta = data.catalog_meta;
            if (intervalInput && !intervalInput.dataset.touched) {
                intervalInput.value = meta.refresh_interval_hours || 6;
            }
            if (topicsInput && !topicsInput.dataset.touched && Array.isArray(meta.topics)) {
                topicsInput.value = meta.topics.join('\n');
            }
            if (allowInvalidInput && !allowInvalidInput.dataset.touched) {
                allowInvalidInput.checked = !!meta.allow_invalid_install;
            }
            if (autoUpdateInput && !autoUpdateInput.dataset.touched) {
                autoUpdateInput.checked = !!meta.auto_update;
            }
            if (tokenInput) {
                // 실제 토큰은 절대 내려주지 않음 — 저장 여부만 표시
                tokenInput.placeholder = meta.github_token_set ? '토큰 저장됨 (변경 시 새 값 입력)' : 'ghp_... (저장 안 됨)';
            }
            if (Array.isArray(meta.gitea_servers)) {
                giteaServers = meta.gitea_servers.map(function(s) {
                    return { url: s.url || '', token: s.token || '' };
                });
                renderGiteaList();
            }
        } catch(e) {
            // 초기값 로드 실패 — 기본값 유지
        }
    }

    // 사용자가 필드를 건드리면 touched 마킹 — 초기값 fetch가 나중에 도착해도 덮어쓰지 않음
    if (intervalInput) intervalInput.addEventListener('input', () => { intervalInput.dataset.touched = '1'; });
    if (topicsInput) topicsInput.addEventListener('input', () => { topicsInput.dataset.touched = '1'; });
    if (allowInvalidInput) allowInvalidInput.addEventListener('change', () => { allowInvalidInput.dataset.touched = '1'; });
    if (autoUpdateInput) autoUpdateInput.addEventListener('change', () => { autoUpdateInput.dataset.touched = '1'; });
    if (tokenInput) tokenInput.addEventListener('input', () => { tokenInput.dataset.touched = '1'; });

    // 토픽 개수 검증 — GitHub 비인증 Search API 분당 10회 제한 보호 (백엔드 _CATALOG_MAX_TOPICS와 동일 규칙)
    function parseTopics(raw) {
        return [...new Set(
            String(raw || '')
                .split(/[\n,]/)
                .map(t => t.trim().toLowerCase())
                .filter(t => /^[a-z0-9][a-z0-9-]*$/.test(t))
        )];
    }

    function showError(msg) {
        if (typeof window.showToast === 'function') {
            window.showToast(msg, 'error');
        } else {
            alert(msg);
        }
    }

    function showSuccess(msg) {
        if (typeof window.showToast === 'function') {
            window.showToast(msg, 'success');
        } else {
            alert(msg);
        }
    }

    // 저장 실행 — script.js saveCatalogSettings와 동일 페이로드/엔드포인트
    async function saveCatalogSettings(form, submitBtn) {
        const intervalVal = intervalInput ? intervalInput.value.trim() : '';
        const topicsVal = topicsInput ? topicsInput.value.trim() : '';

        const uniqueTopics = parseTopics(topicsVal);
        if (uniqueTopics.length > 5) {
            showError('토픽은 최대 5개까지 등록할 수 있습니다. (현재 ' + uniqueTopics.length + '개) — GitHub 비인증 검색은 분당 10회 제한입니다.');
            if (submitBtn) { submitBtn.disabled = false; }
            return;
        }

        const origHtml = submitBtn ? submitBtn.innerHTML : '';
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 저장 중...';
        }

        try {
            const res = await fetch('/api/media/dashboard/widgets/plugin_manager/save-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: 'general',
                    refresh_interval_hours: intervalVal,
                    topics: topicsVal,
                    allow_invalid_install: allowInvalidInput ? allowInvalidInput.checked : false,
                    auto_update: autoUpdateInput ? autoUpdateInput.checked : false,
                    github_token: tokenInput ? tokenInput.value.trim() : '',
                    gitea_servers: giteaServers
                })
            });
            const data = await res.json();
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = origHtml;
            }
            if (data.success) {
                showSuccess(data.message || '카탈로그 설정이 저장되었습니다.');
            } else {
                showError(data.error || '설정 저장 실패');
            }
        } catch (err) {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.innerHTML = origHtml;
            }
            showError('통신 오류: ' + err.message);
        }
    }

    // 코어 폼 submit 가로채기 — capture 단계 (코어 핸들러는 bubble 단계라 나중에 실행)
    const form = root.closest('form.plugin-config-form');
    if (form) {
        form.addEventListener('submit', function(e) {
            const submitBtn = form.querySelector('button[type="submit"]');
            e.preventDefault();
            e.stopImmediatePropagation();
            saveCatalogSettings(form, submitBtn);
        }, true); // capture
    }

    loadInitial();

    // Gitea 서버 추가 — URL 검증 후 로컬 목록에 추가 (저장은 설정 저장 시)
    if (giteaAddBtn) {
        giteaAddBtn.addEventListener('click', function() {
            const url = giteaUrlInput ? giteaUrlInput.value.trim() : '';
            const token = giteaTokenInput ? giteaTokenInput.value.trim() : '';
            if (!url) {
                showError('Gitea 서버 URL을 입력하세요.');
                return;
            }
            let norm;
            try {
                const u = new URL(url);
                if (u.protocol !== 'https:') throw new Error('https only');
                norm = u.origin;
            } catch(e) {
                showError('올바른 https:// URL을 입력하세요. (예: https://git.example.com)');
                return;
            }
            // 중복 host 체크
            const dup = giteaServers.some(function(s) {
                try { return new URL(s.url).host === new URL(norm).host; }
                catch(e) { return false; }
            });
            if (dup) {
                showError('이미 등록된 Gitea 서버입니다.');
                return;
            }
            giteaServers.push({ url: norm, token: token });
            if (giteaUrlInput) giteaUrlInput.value = '';
            if (giteaTokenInput) giteaTokenInput.value = '';
            renderGiteaList();
        });
    }

    // 토큰 삭제 — 즉시 clear 요청 (빈 입력은 기존 유지라 별도 삭제 경로)
    if (tokenClearBtn) {
        tokenClearBtn.addEventListener('click', async function() {
            if (!tokenInput) return;
            if (typeof window.confirm === 'function' && !confirm('저장된 GitHub 토큰을 삭제할까요?')) return;
            try {
                const res = await fetch('/api/media/dashboard/widgets/plugin_manager/save-config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ type: 'general', clear_github_token: true })
                });
                const data = await res.json();
                if (data.success) {
                    tokenInput.value = '';
                    tokenInput.placeholder = 'ghp_... (저장 안 됨)';
                    showSuccess(data.message || 'GitHub 토큰이 삭제되었습니다.');
                } else {
                    showError(data.error || '토큰 삭제 실패');
                }
            } catch (err) {
                showError('통신 오류: ' + err.message);
            }
        });
    }
})(window, pluginId, root, config);
