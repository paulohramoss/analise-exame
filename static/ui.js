/* Three Health — utilitários de interface compartilhados (toasts e cópia de texto). */
(function () {
    'use strict';

    const ICONS = {
        success: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
        error: '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
        warning: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
        info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    };

    function region() {
        let el = document.getElementById('toast-region');
        if (!el) {
            el = document.createElement('div');
            el.id = 'toast-region';
            el.className = 'toast-region';
            el.setAttribute('role', 'status');
            el.setAttribute('aria-live', 'polite');
            document.body.appendChild(el);
        }
        return el;
    }

    /**
     * Mostra um aviso temporário no canto da tela.
     * @param {string} message
     * @param {'success'|'error'|'warning'|'info'} [type]
     * @param {number} [duration] em ms
     */
    function showToast(message, type = 'success', duration = 3500) {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;

        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        ['width', 'height'].forEach(attr => svg.setAttribute(attr, '18'));
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        svg.setAttribute('aria-hidden', 'true');
        svg.classList.add('icon');
        svg.innerHTML = ICONS[type] || ICONS.info;

        const text = document.createElement('span');
        text.textContent = message;

        toast.append(svg, text);
        region().appendChild(toast);

        requestAnimationFrame(() => toast.classList.add('is-visible'));
        setTimeout(() => {
            toast.classList.remove('is-visible');
            setTimeout(() => toast.remove(), 250);
        }, duration);
    }

    /** Copia texto para a área de transferência, com alternativa para navegadores antigos. */
    function copyToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise((resolve, reject) => {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            ta.remove();
            ok ? resolve() : reject(new Error('copy failed'));
        });
    }

    window.showToast = showToast;
    window.copyToClipboard = copyToClipboard;
})();
