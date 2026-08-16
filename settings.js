// plugins/metadata/plugin_manager/settings.js
// 코어 openSettingsModal이 new Function('window','pluginId','root','config')로 실행
// 저장은 공용 모달 하단 "설정 저장" 버튼 사용 (script.js saveSettingsModal → plugin_manager 분기)
// 여기서는 초기값 로드만 담당
(function(window, pluginId, root, config) {
    'use strict';

    if (!root) {
        root = document.querySelector('[data-plugin-settings-root="plugin_manager"]');
    }
    if (!root) return;

    const intervalInput = root.querySelector('#pm-catalog-interval');
    const topicsInput = root.querySelector('#pm-catalog-topics');
    const allowInvalidInput = root.querySelector('#pm-allow-invalid-install');
    const cvisEnabledInput = root.querySelector('#pm-cvis-enabled');

    // 초기값 로드 — /data 응답의 catalog_meta (간격/토픽은 MariaDB 설정)
    async function loadInitial() {
        try {
            const res = await fetch('/api/media/dashboard/widgets/plugin_manager/data?type=general');
            const data = await res.json();
            if (!data.success || !data.catalog_meta) return;
            const meta = data.catalog_meta;
            if (intervalInput) {
                intervalInput.value = meta.refresh_interval_hours || 6;
            }
            if (topicsInput && Array.isArray(meta.topics)) {
                topicsInput.value = meta.topics.join('\n');
            }
            if (allowInvalidInput) {
                allowInvalidInput.checked = !!meta.allow_invalid_install;
            }
            if (cvisEnabledInput) {
                cvisEnabledInput.checked = !!meta.category_vis_enabled;
            }
        } catch(e) {
            // 초기값 로드 실패 — 기본값 유지
        }
    }

    loadInitial();
})(window, pluginId, root, config);
