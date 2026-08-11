// plugins/metadata/plugin_manager/script.js
(function() {
    console.log('[PluginManager] Fullpage UI Script Loaded.');

    let allPlugins = [];
    let currentFilter = 'all';
    let currentSearch = '';
    let pendingDeletePluginId = null;

    // 🎨 테마 감지 (MutationObserver - 가이드 규격)
    function getCurrentTheme() {
        return document.documentElement.getAttribute('data-app-theme') || 'purple';
    }

    const themeObserver = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            if (mutation.type === 'attributes' && mutation.attributeName === 'data-app-theme') {
                console.log(`[PluginManager] Theme changed to: ${getCurrentTheme()}`);
            }
        });
    });

    themeObserver.observe(document.documentElement, { attributes: true });

    // Toast/Alert 표시
    function showAlert(msg, isError = false) {
        const banner = document.getElementById('pm-alert-banner');
        const msgEl = document.getElementById('pm-alert-message');
        if (!banner || !msgEl) return;

        msgEl.textContent = msg;
        banner.className = 'pm-alert ' + (isError ? 'pm-alert-error' : 'pm-alert-success');
        banner.style.display = 'flex';

        setTimeout(() => {
            if (banner.style.display !== 'none') {
                banner.style.display = 'none';
            }
        }, 5000);
    }

    // Backend API 호출 헬퍼 (apply-metadata 액션)
    function callPluginAction(actionData) {
        return fetch('/api/media/books/0/apply-metadata', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'general',
                source: 'plugin_manager',
                item_data: actionData
            })
        }).then(res => res.json());
    }

    // 플러그인 목록 조회
    function loadPlugins() {
        const grid = document.getElementById('pm-plugins-grid');
        if (grid) {
            grid.innerHTML = `
                <div class="pm-loading-state">
                    <i class="fa-solid fa-circle-notch fa-spin"></i>
                    <p>설치된 플러그인 목록을 조회하는 중입니다...</p>
                </div>
            `;
        }

        fetch('/api/media/dashboard/widgets/plugin_manager/data?type=general')
            .then(res => res.json())
            .then(data => {
                if (data.success && Array.isArray(data.plugins)) {
                    allPlugins = data.plugins;
                    updateCounts();
                    renderPlugins();
                    checkUpdatesAsync();
                } else {
                    showAlert(data.error || '플러그인 목록을 불러올 수 없습니다.', true);
                }
            })
            .catch(err => {
                showAlert('플러그인 목록 통신 오류: ' + err.message, true);
            });
    }

    // 업데이트 버튼 이벤트 바인딩 (renderPlugins + patchCardUpdate 공용)
    // escapeHtml 로컬 폴백 (전역 미정의 대비)
    const escapeHtml = (typeof window.escapeHtml === 'function')
        ? window.escapeHtml
        : function(v) {
            return String(v == null ? '' : v)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        };

    function bindUpdateButton(btn) {
        btn.addEventListener('click', async function(e) {
            e.preventDefault();
            e.stopPropagation();

            const pluginId = this.getAttribute('data-id');
            const pluginName = this.getAttribute('data-name');
            if (!pluginId) return;

            const origHtml = this.innerHTML;
            this.disabled = true;
            this.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 업데이트 중...';

            try {
                const res = await callPluginAction({ action: 'update', plugin_id: pluginId });
                this.disabled = false;
                this.innerHTML = origHtml;
                if (res.success) {
                    showAlert(res.message || `'${pluginName}' 플러그인이 최신 버전으로 업데이트되었습니다!`);
                    loadPlugins();
                } else {
                    showAlert(res.error || '플러그인 업데이트 실패', true);
                }
            } catch(err) {
                this.disabled = false;
                this.innerHTML = origHtml;
                showAlert('업데이트 중 통신 오류가 발생했습니다: ' + err.message, true);
            }
        });
    }

    // 업데이트 체크 비동기 진행 (목록 렌더 이후 개별 조회)
    let updateCheckSeq = 0;
    function checkUpdatesAsync() {
        const seq = ++updateCheckSeq; // loadPlugins 재호출 시 이전 배치 결과 무시
        const targets = allPlugins.filter(p => p.has_update_manifest);
        if (targets.length === 0) return;

        const CONCURRENCY = 3;
        const queue = targets.slice();

        function markChecking(p, checking) {
            const idEl = document.querySelector(`#pm-card-${CSS.escape(p.id)} .pm-plugin-id`);
            if (!idEl) return;
            const spinner = idEl.querySelector('.pm-update-spinner');
            if (checking && !spinner) {
                idEl.insertAdjacentHTML('beforeend',
                    ' <i class="pm-update-spinner fa-solid fa-circle-notch fa-spin" title="업데이트 확인 중" style="opacity:0.6;"></i>');
            } else if (!checking && spinner) {
                spinner.remove();
            }
        }

        function next() {
            if (seq !== updateCheckSeq) return;
            const p = queue.shift();
            if (!p) return;
            markChecking(p, true);
            callPluginAction({ action: 'check_update', plugin_id: p.id })
                .then(res => {
                    if (seq !== updateCheckSeq) return;
                    markChecking(p, false);
                    // 성공 시 message에 결과 객체가 담김
                    const r = res && res.success ? res.message : null;
                    if (r && typeof r === 'object') {
                        patchCardUpdate(r.plugin_id, !!r.has_update, r.latest_version);
                    }
                })
                .catch(() => { markChecking(p, false); })
                .finally(() => { next(); });
        }

        for (let i = 0; i < CONCURRENCY; i++) next();
    }

    // 개별 카드 업데이트 상태 반영 (부분 DOM 패치)
    function patchCardUpdate(pluginId, hasUpdate, latestVersion) {
        const p = allPlugins.find(x => x.id === pluginId);
        if (!p) return;
        p.has_update = hasUpdate;
        p.latest_version = latestVersion;
        if (!hasUpdate) return;

        const card = document.getElementById(`pm-card-${pluginId}`);
        if (!card) return; // 필터로 숨겨진 상태 — 데이터만 갱신
        const actions = card.querySelector('.pm-card-action-btns');
        if (!actions || actions.querySelector('.pm-btn-update')) return;

        const btnHtml = `<button class="pm-btn pm-btn-warning pm-btn-sm pm-btn-update" data-id="${p.id}" data-name="${escapeHtml(p.name)}" title="최신 버전으로 업데이트 (v${escapeHtml(latestVersion)})" style="background: rgba(245, 158, 11, 0.2); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.45); font-size: 0.76rem; padding: 0.35rem 0.65rem; border-radius: 6px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 0.3rem;">
            <i class="fa-solid fa-arrow-up-from-bracket"></i> 업데이트 (v${escapeHtml(latestVersion)})
           </button>`;
        actions.insertAdjacentHTML('afterbegin', btnHtml);

        const btn = actions.querySelector('.pm-btn-update');
        if (btn) bindUpdateButton(btn);
    }

    // 카운트 배지 업데이트
    function updateCounts() {
        const countAll = document.getElementById('pm-count-all');
        const countEnabled = document.getElementById('pm-count-enabled');
        const countDisabled = document.getElementById('pm-count-disabled');

        if (countAll) countAll.textContent = allPlugins.length;
        if (countEnabled) countEnabled.textContent = allPlugins.filter(p => p.enabled).length;
        if (countDisabled) countDisabled.textContent = allPlugins.filter(p => !p.enabled).length;
    }

    // 카드 렌더링
    function renderPlugins() {
        const grid = document.getElementById('pm-plugins-grid');
        if (!grid) return;

        let filtered = allPlugins.filter(p => {
            // Tab filter
            if (currentFilter === 'enabled' && !p.enabled) return false;
            if (currentFilter === 'disabled' && p.enabled) return false;
            if (currentFilter === 'category' && !p.is_category) return false;
            if (currentFilter === 'widget' && !p.is_widget) return false;

            // Search text filter
            if (currentSearch) {
                const q = currentSearch.toLowerCase();
                const matchName = (p.name || '').toLowerCase().includes(q);
                const matchId = (p.id || '').toLowerCase().includes(q);
                return matchName || matchId;
            }

            return true;
        });

        if (filtered.length === 0) {
            grid.innerHTML = `
                <div class="pm-loading-state">
                    <i class="fa-solid fa-folder-open" style="font-size: 2.5rem; opacity: 0.5;"></i>
                    <p>조건에 일치하는 플러그인이 없습니다.</p>
                </div>
            `;
            return;
        }

        let html = '';
        filtered.forEach(p => {
            const systemBadge = p.is_system
                ? '<span class="pm-badge pm-badge-system">SYSTEM</span>'
                : '';

            const originBadge = '<span class="pm-badge pm-badge-local"><i class="fa-solid fa-folder"></i> 로컬 플러그인</span>';

            const categoryBadge = p.is_category
                ? '<span class="pm-badge pm-badge-feature"><i class="fa-solid fa-layer-group"></i> 카테고리 뷰</span>'
                : '';

            const widgetBadge = p.is_widget
                ? '<span class="pm-badge pm-badge-feature"><i class="fa-solid fa-chart-simple"></i> 대시보드 위젯</span>'
                : '';

            const searchableBadge = p.is_searchable
                ? '<span class="pm-badge pm-badge-feature"><i class="fa-solid fa-magnifying-glass"></i> 수동 검색</span>'
                : '';

            const updateBtnHtml = (p.has_update && (p.has_update_manifest || !p.is_system))
                ? `<button class="pm-btn pm-btn-warning pm-btn-sm pm-btn-update" data-id="${p.id}" data-name="${p.name}" title="최신 버전으로 업데이트 (v${p.latest_version})" style="background: rgba(245, 158, 11, 0.2); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.45); font-size: 0.76rem; padding: 0.35rem 0.65rem; border-radius: 6px; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 0.3rem;">
                    <i class="fa-solid fa-arrow-up-from-bracket"></i> 업데이트 (v${escapeHtml(p.latest_version)})
                   </button>`
                : '';

            const deleteBtnHtml = p.is_system
                ? ''
                : `<button class="pm-btn pm-btn-secondary pm-btn-sm pm-btn-delete" data-id="${p.id}" data-name="${p.name}" title="삭제">
                    <i class="fa-solid fa-trash-can pm-text-danger"></i>
                   </button>`;

            const settingsBtnHtml = p.has_config
                ? `<button class="pm-btn pm-btn-secondary pm-btn-sm pm-btn-icon-only pm-btn-settings" data-id="${p.id}" data-name="${p.name}" title="설정">
                    <i class="fa-solid fa-gear"></i>
                   </button>`
                : '';

            const clickableClass = '';
            const gitUrlAttr = '';

            html += `
                <div class="pm-plugin-card ${clickableClass}" id="pm-card-${p.id}" data-id="${p.id}" ${gitUrlAttr}>
                    <div>
                        <div class="pm-plugin-top">
                            <div class="pm-plugin-icon-title">
                                <div class="pm-plugin-avatar">
                                    <i class="${p.is_category ? 'fa-solid fa-boxes-stacked' : (p.is_widget ? 'fa-solid fa-chart-column' : 'fa-solid fa-puzzle-piece')}"></i>
                                </div>
                                <div>
                                    <h4 class="pm-plugin-name">${p.name}</h4>
                                    <span class="pm-plugin-id">${p.id} • v${p.version}</span>
                                </div>
                            </div>
                            ${settingsBtnHtml}
                        </div>

                        <div class="pm-badges-row">
                            ${originBadge}
                            ${systemBadge}
                            ${categoryBadge}
                            ${widgetBadge}
                            ${searchableBadge}
                        </div>
                    </div>

                    <div class="pm-plugin-footer">
                        <div class="pm-toggle-wrap">
                            <label class="pm-switch">
                                <input type="checkbox" class="pm-toggle-input" data-id="${p.id}" ${p.enabled ? 'checked' : ''}>
                                <span class="pm-slider"></span>
                            </label>
                            <span>${p.enabled ? '사용 중' : '중지됨'}</span>
                        </div>
                        <div class="pm-card-action-btns">
                            ${updateBtnHtml}
                            ${deleteBtnHtml}
                        </div>
                    </div>
                </div>
            `;
        });

        grid.innerHTML = html;
        bindCardEvents();
    }

    // 카드 이벤트 연결
    function bindCardEvents() {
        // Toggle Switch
        document.querySelectorAll('.pm-toggle-input').forEach(input => {
            input.addEventListener('change', function(e) {
                e.stopPropagation();
                const pluginId = this.getAttribute('data-id');
                const enabledVal = this.checked ? '1' : '0';

                callPluginAction({ action: 'toggle', plugin_id: pluginId, enabled: enabledVal })
                    .then(res => {
                        if (res.success) {
                            showAlert(res.message || '상태가 변경되었습니다.');
                            loadPlugins();
                            if (typeof window.loadLibraries === 'function') {
                                window.loadLibraries();
                            }
                            if (typeof window.invalidateMetadataPluginsCache === 'function') {
                                window.invalidateMetadataPluginsCache();
                            }
                        } else {
                            showAlert(res.error || '상태 변경 실패', true);
                            this.checked = !this.checked;
                        }
                    })
                    .catch(err => {
                        showAlert('통신 오류: ' + err.message, true);
                        this.checked = !this.checked;
                    });
            });
        });

        // Update Button
        document.querySelectorAll('.pm-btn-update').forEach(bindUpdateButton);

        // Settings Button (카드 우상단 톱니바퀴)
        document.querySelectorAll('.pm-btn-settings').forEach(btn => {
            btn.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                const pluginId = this.getAttribute('data-id');
                if (pluginId) {
                    openSettingsModal(pluginId);
                }
            });
        });

        // Delete Button
        document.querySelectorAll('.pm-btn-delete').forEach(btn => {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                const pluginId = this.getAttribute('data-id');
                const pluginName = this.getAttribute('data-name');
                openDeleteModal(pluginId, pluginName);
            });
        });
    }

    // Settings Modal Control
    async function openSettingsModal(pluginId) {
        const modal = document.getElementById('pm-settings-modal');
        const titleEl = document.getElementById('pm-settings-modal-title');
        const bodyEl = document.getElementById('pm-settings-modal-body');
        const extraLinksEl = document.getElementById('pm-settings-extra-links');
        if (!modal || !bodyEl) return;

        bodyEl.innerHTML = '<div style="text-align: center; padding: 2rem; color: var(--app-text-muted, #94a3b8);"><i class="fa-solid fa-spinner fa-spin fa-2x"></i><p style="margin-top: 0.8rem;">설정 정보를 불러오는 중...</p></div>';
        if (extraLinksEl) extraLinksEl.innerHTML = '';
        modal.style.display = 'flex';

        try {
            const res = await fetch('/api/media/metadata/plugins/manage');
            const data = await res.json();
            if (!data.success || !data.plugins) {
                throw new Error(data.error || '플러그인 설정 정보를 가져오지 못했습니다.');
            }

            const p = data.plugins.find(item => item.id === pluginId);
            if (!p) {
                throw new Error('선택한 플러그인 정보를 찾을 수 없습니다.');
            }

            if (titleEl) {
                titleEl.textContent = `${p.name} (${p.id}) v${p.version || '1.0.0'}`;
            }

            if (extraLinksEl) {
                extraLinksEl.innerHTML = '';
            }

            const schema = p.config_schema || [];
            const config = p.config || {};
            const hasCustomUi = !!(p.settings_ui && p.settings_ui.html);

            let formHtml = '';

            if (hasCustomUi) {
                const escapedConfig = escapeHtmlAttr(JSON.stringify(config));
                formHtml = `
                    <form id="pm-settings-form" data-plugin-id="${p.id}">
                        <div class="plugin-settings-ui-root" data-plugin-settings-root="${p.id}" data-plugin-config='${escapedConfig}'>
                            ${p.settings_ui.html}
                        </div>
                    </form>
                `;
            } else if (schema.length > 0) {
                formHtml = `
                    <form id="pm-settings-form" data-plugin-id="${p.id}" style="display: flex; flex-direction: column; gap: 1rem;">
                        ${schema.map(f => renderSchemaField(f, config[f.key])).join('')}
                    </form>
                `;
            } else {
                formHtml = `
                    <div style="text-align: center; padding: 2rem; color: var(--app-text-muted, #94a3b8);">
                        <i class="fa-solid fa-sliders fa-2x" style="margin-bottom: 0.8rem; color: var(--app-accent, #6366f1); opacity: 0.8;"></i>
                        <p style="margin: 0; font-size: 0.95rem;">이 플러그인은 별도의 추가 설정 항목이 없습니다.</p>
                    </div>
                `;
            }

            bodyEl.innerHTML = formHtml;

            if (hasCustomUi) {
                const rootEl = bodyEl.querySelector(`[data-plugin-settings-root="${p.id}"]`);
                if (rootEl) {
                    let pluginConfig = p.config || config || {};
                    try {
                        const rawConfig = rootEl.dataset.pluginConfig;
                        if (rawConfig) pluginConfig = JSON.parse(rawConfig);
                    } catch(e) {}

                    if (p.settings_ui && p.settings_ui.css) {
                        const styleEl = document.createElement('style');
                        styleEl.textContent = p.settings_ui.css;
                        rootEl.appendChild(styleEl);
                    }

                    if (p.settings_ui && p.settings_ui.js) {
                        try {
                            const fn = new Function('window', 'pluginId', 'root', 'config', p.settings_ui.js);
                            fn(window, p.id, rootEl, pluginConfig);
                        } catch(e) {
                            console.error(`[PluginManager] Custom UI script execution error for ${p.id}:`, e);
                        }
                    }

                    const inlineScripts = rootEl.querySelectorAll('script');
                    inlineScripts.forEach(script => {
                        try {
                            const fn = new Function('window', 'pluginId', 'root', 'config', script.textContent);
                            fn(window, p.id, rootEl, pluginConfig);
                        } catch(e) {
                            console.error(`[PluginManager] Custom UI inline script error for ${p.id}:`, e);
                        }
                    });
                }
            }

        } catch (err) {
            bodyEl.innerHTML = `<div style="text-align: center; padding: 2rem; color: #ef4444;"><i class="fa-solid fa-triangle-exclamation fa-2x"></i><p style="margin-top: 0.8rem;">${err.message}</p></div>`;
        }
    }

    function escapeHtmlAttr(value) {
        return String(value || '')
            .replace(/&/g, '&')
            .replace(/\"/g, '"')
            .replace(/'/g, '\'')
            .replace(/</g, '<')
            .replace(/>/g, '>');
    }

    function renderSchemaField(f, curVal) {
        const label = f.label || f.key;
        const required = !!f.required;
        const descHtml = f.description ? `<p style="font-size: 0.78rem; color: var(--app-text-muted, #94a3b8); margin: 0.3rem 0 0 0;">${f.description}</p>` : '';
        const key = f.key || '';
        const type = (f.type || 'text').toLowerCase();

        if (type === 'checkbox') {
            const checked = curVal === true || curVal === '1' || curVal === 1 || curVal === 'true';
            return `
                <div style="display: flex; flex-direction: column; gap: 0.3rem;">
                    <label style="font-weight: 600; color: var(--app-text-primary, #fff); font-size: 0.88rem;">
                        ${label} ${required ? '<span style="color:#f43f5e;">*</span>' : ''}
                    </label>
                    <label style="display:flex; align-items:center; gap:0.5rem; color: var(--app-text-secondary, #cbd5e1); cursor: pointer;">
                        <input type="checkbox" name="${key}" ${checked ? 'checked' : ''} style="width: 16px; height: 16px;">
                        <span>사용</span>
                    </label>
                    ${descHtml}
                </div>
            `;
        }

        if (type === 'select') {
            const options = Array.isArray(f.options) ? f.options : [];
            const cur = curVal ?? f.default ?? '';
            return `
                <div style="display: flex; flex-direction: column; gap: 0.3rem;">
                    <label style="font-weight: 600; color: var(--app-text-primary, #fff); font-size: 0.88rem;">
                        ${label} ${required ? '<span style="color:#f43f5e;">*</span>' : ''}
                    </label>
                    <select name="${key}" style="width: 100%; padding: 0.5rem; border-radius: 6px; background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(255, 255, 255, 0.15); color: #fff; font-size: 0.88rem;">
                        ${options.map(opt => {
                            const val = typeof opt === 'object' ? opt.value : opt;
                            const name = typeof opt === 'object' ? opt.label : opt;
                            const sel = String(val) === String(cur) ? 'selected' : '';
                            return `<option value="${val}" ${sel}>${name}</option>`;
                        }).join('')}
                    </select>
                    ${descHtml}
                </div>
            `;
        }

        const val = curVal ?? f.default ?? '';
        return `
            <div style="display: flex; flex-direction: column; gap: 0.3rem;">
                <label style="font-weight: 600; color: var(--app-text-primary, #fff); font-size: 0.88rem;">
                    ${label} ${required ? '<span style="color:#f43f5e;">*</span>' : ''}
                </label>
                <input type="${type === 'password' ? 'password' : 'text'}" name="${key}" value="${val}" style="width: 100%; padding: 0.5rem 0.8rem; border-radius: 6px; background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(255, 255, 255, 0.15); color: #fff; font-size: 0.88rem;" />
                ${descHtml}
            </div>
        `;
    }

    function closeSettingsModal() {
        const modal = document.getElementById('pm-settings-modal');
        if (modal) modal.style.display = 'none';
    }

    async function saveSettingsModal() {
        const form = document.getElementById('pm-settings-form');
        if (!form) {
            closeSettingsModal();
            return;
        }

        const pluginId = form.dataset.pluginId;
        const configData = {};
        const inputs = form.querySelectorAll('input, select');
        inputs.forEach(inp => {
            if (inp.name) {
                if (inp.type === 'checkbox') {
                    configData[inp.name] = !!inp.checked;
                } else {
                    configData[inp.name] = String(inp.value ?? '').trim();
                }
            }
        });

        const saveBtn = document.getElementById('pm-settings-modal-save-btn');
        const origHtml = saveBtn ? saveBtn.innerHTML : '';
        if (saveBtn) {
            saveBtn.disabled = true;
            saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 저장 중...';
        }

        try {
            const res = await fetch('/api/media/metadata/plugins/save-config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    type: 'general',
                    plugin_id: pluginId,
                    config: configData
                })
            });
            const data = await res.json();
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.innerHTML = origHtml;
            }
            if (data.success) {
                showAlert(data.message || '플러그인 설정이 저장되었습니다.');
                closeSettingsModal();
                loadPlugins();
            } else {
                showAlert(data.error || '설정 저장 실패', true);
            }
        } catch (err) {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.innerHTML = origHtml;
            }
            showAlert('통신 오류: ' + err.message, true);
        }
    }

    // Modal Control
    function openDeleteModal(pluginId, pluginName) {
        pendingDeletePluginId = pluginId;
        const nameEl = document.getElementById('pm-delete-plugin-name');
        const modal = document.getElementById('pm-delete-modal');
        if (nameEl) nameEl.textContent = `${pluginName} (${pluginId})`;
        if (modal) modal.style.display = 'flex';
    }

    function closeDeleteModal() {
        pendingDeletePluginId = null;
        const modal = document.getElementById('pm-delete-modal');
        if (modal) modal.style.display = 'none';
    }

    // Event Listeners Setup
    function initEvents() {
        // Refresh Button
        const refreshBtn = document.getElementById('pm-btn-refresh');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => loadPlugins());
        }

        // ZIP Install Button & File Selector
        const zipInput = document.getElementById('pm-zip-file-input');
        const selectZipBtn = document.getElementById('pm-btn-select-zip');
        const zipLabel = document.getElementById('pm-zip-file-label');
        const zipInstallBtn = document.getElementById('pm-btn-zip-install');

        if (selectZipBtn && zipInput) {
            selectZipBtn.addEventListener('click', () => zipInput.click());
            zipInput.addEventListener('change', (e) => {
                const file = e.target.files && e.target.files[0];
                if (file) {
                    if (zipLabel) zipLabel.textContent = file.name;
                } else {
                    if (zipLabel) zipLabel.textContent = 'ZIP 압축 파일 선택...';
                }
            });
        }

        if (zipInstallBtn && zipInput) {
            zipInstallBtn.addEventListener('click', () => {
                const file = zipInput.files && zipInput.files[0];
                if (!file) {
                    showAlert('업로드할 ZIP 압축 파일을 선택해주세요.', true);
                    return;
                }

                if (!file.name.toLowerCase().endsWith('.zip')) {
                    showAlert('.zip 확장자 파일만 업로드할 수 있습니다.', true);
                    return;
                }

                const originalHtml = zipInstallBtn.innerHTML;
                zipInstallBtn.disabled = true;
                zipInstallBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 업로드 중...';

                const reader = new FileReader();
                reader.onload = function(e) {
                    const base64Data = e.target.result;
                    callPluginAction({ action: 'install_zip', zip_data: base64Data, filename: file.name })
                        .then(res => {
                            zipInstallBtn.disabled = false;
                            zipInstallBtn.innerHTML = originalHtml;
                            if (res.success) {
                                showAlert(res.message || 'ZIP 플러그인이 설치되었습니다!');
                                zipInput.value = '';
                                if (zipLabel) zipLabel.textContent = 'ZIP 압축 파일 선택...';
                                loadPlugins();
                            } else {
                                showAlert(res.error || 'ZIP 플러그인 설치 실패', true);
                            }
                        })
                        .catch(err => {
                            zipInstallBtn.disabled = false;
                            zipInstallBtn.innerHTML = originalHtml;
                            showAlert('ZIP 설치 중 통신 오류: ' + err.message, true);
                        });
                };
                reader.onerror = function() {
                    zipInstallBtn.disabled = false;
                    zipInstallBtn.innerHTML = originalHtml;
                    showAlert('파일 읽기 실패', true);
                };
                reader.readAsDataURL(file);
            });
        }

        // Git URL Install
        const gitUrlInput = document.getElementById('pm-git-url-input');
        const gitInstallBtn = document.getElementById('pm-btn-git-install');

        if (gitInstallBtn && gitUrlInput) {
            const runGitInstall = () => {
                const url = (gitUrlInput.value || '').trim();
                if (!url) {
                    showAlert('Git 저장소 URL을 입력해주세요.', true);
                    gitUrlInput.focus();
                    return;
                }
                if (!/^https?:\/\//i.test(url)) {
                    showAlert('유효한 Git 저장소 URL이 아닙니다. (http/https URL만 허용)', true);
                    return;
                }

                const originalHtml = gitInstallBtn.innerHTML;
                gitInstallBtn.disabled = true;
                gitInstallBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 설치 중...';

                callPluginAction({ action: 'install_git', git_url: url })
                    .then(res => {
                        gitInstallBtn.disabled = false;
                        gitInstallBtn.innerHTML = originalHtml;
                        if (res.success) {
                            showAlert(res.message || 'Git 플러그인이 설치되었습니다!');
                            gitUrlInput.value = '';
                            loadPlugins();
                        } else {
                            showAlert(res.error || 'Git 플러그인 설치 실패', true);
                        }
                    })
                    .catch(err => {
                        gitInstallBtn.disabled = false;
                        gitInstallBtn.innerHTML = originalHtml;
                        showAlert('Git 설치 중 통신 오류: ' + err.message, true);
                    });
            };

            gitInstallBtn.addEventListener('click', runGitInstall);
            gitUrlInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') runGitInstall();
            });
        }

        // Tabs Filter
        document.querySelectorAll('.pm-tab-btn').forEach(btn => {
            btn.addEventListener('click', function() {
                document.querySelectorAll('.pm-tab-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                currentFilter = this.getAttribute('data-filter') || 'all';
                renderPlugins();
            });
        });

        // Search Input
        const searchInput = document.getElementById('pm-search-input');
        if (searchInput) {
            searchInput.addEventListener('input', function() {
                currentSearch = (this.value || '').trim();
                renderPlugins();
            });
        }

        // Delete Modal buttons
        const closeBtn = document.getElementById('pm-modal-close-btn');
        const cancelBtn = document.getElementById('pm-modal-cancel-btn');
        const confirmBtn = document.getElementById('pm-modal-confirm-delete-btn');

        if (closeBtn) closeBtn.addEventListener('click', closeDeleteModal);
        if (cancelBtn) cancelBtn.addEventListener('click', closeDeleteModal);
        if (confirmBtn) {
            confirmBtn.addEventListener('click', () => {
                if (!pendingDeletePluginId) return;

                const originalHtml = confirmBtn.innerHTML;
                confirmBtn.disabled = true;
                confirmBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 삭제 중...';

                callPluginAction({ action: 'delete', plugin_id: pendingDeletePluginId })
                    .then(res => {
                        confirmBtn.disabled = false;
                        confirmBtn.innerHTML = originalHtml;
                        closeDeleteModal();
                        if (res.success) {
                            showAlert(res.message || '플러그인이 삭제되었습니다.');
                            loadPlugins();
                        } else {
                            showAlert(res.error || '삭제 실패', true);
                        }
                    })
                    .catch(err => {
                        confirmBtn.disabled = false;
                        confirmBtn.innerHTML = originalHtml;
                        closeDeleteModal();
                        showAlert('통신 오류: ' + err.message, true);
                    });
            });
        }

        // Settings Modal buttons
        const settingsCloseBtn = document.getElementById('pm-settings-modal-close-btn');
        const settingsCancelBtn = document.getElementById('pm-settings-modal-cancel-btn');
        const settingsSaveBtn = document.getElementById('pm-settings-modal-save-btn');

        if (settingsCloseBtn) settingsCloseBtn.addEventListener('click', closeSettingsModal);
        if (settingsCancelBtn) settingsCancelBtn.addEventListener('click', closeSettingsModal);
        if (settingsSaveBtn) settingsSaveBtn.addEventListener('click', saveSettingsModal);
    }

    // Initial Start
    initEvents();
    loadPlugins();
})();