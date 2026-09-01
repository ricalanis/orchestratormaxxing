/**
 * Dashboard auth injector — auto-attaches `Authorization: Bearer <token>`
 * to all mutating fetch calls (POST/PATCH/DELETE/PUT).
 *
 * The token is injected by the server via the DASHBOARD_TOKEN Jinja2 global
 * into a <meta> tag. This script reads it and monkey-patches fetch so every
 * existing JS call site works without modification.
 */
(function () {
    const meta = document.querySelector('meta[name="dashboard-token"]');
    let token = meta ? meta.getAttribute('content') : '';

    const MUTATING = new Set(['POST', 'PATCH', 'DELETE', 'PUT']);
    const originalFetch = window.fetch;

    function requestMethod(input, init) {
        return ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    }

    function requestHeaders(input, init) {
        if (init && init.headers !== undefined) return new Headers(init.headers);
        if (input && input.headers !== undefined) return new Headers(input.headers);
        return new Headers();
    }

    function withToken(input, init, value) {
        if (!value) return init;
        const next = { ...(init || {}) };
        const headers = requestHeaders(input, init);
        headers.set('Authorization', 'Bearer ' + value);
        next.headers = headers;
        return next;
    }

    function isSameOriginApi(input) {
        try {
            const raw = typeof input === 'string' || input instanceof URL
                ? input : input.url;
            const url = new URL(raw, window.location.href);
            return url.origin === window.location.origin
                && (url.pathname === '/api' || url.pathname.startsWith('/api/'));
        } catch (e) { return false; }
    }

    // A tab can survive a service restart or token rotation.  The rejected 401
    // is side-effect free because auth runs before every API handler, so read the
    // freshly rendered token and let the caller replay exactly once.  Use the
    // original fetch here: the bootstrap GET must never recurse through us.
    async function refreshToken() {
        try {
            const response = await originalFetch.call(window, window.location.origin + '/', {
                method: 'GET', cache: 'no-store', headers: { 'Accept': 'text/html' },
            });
            if (!response.ok) return '';
            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, 'text/html');
            const fresh = doc.querySelector('meta[name="dashboard-token"]')
                ?.getAttribute('content') || '';
            if (fresh) {
                token = fresh;
                if (meta) meta.setAttribute('content', fresh);
            }
            return fresh;
        } catch (e) { return ''; }
    }

    window.fetch = async function (input, init) {
        const method = requestMethod(input, init);
        const mutating = MUTATING.has(method);
        const callerAuthorized = requestHeaders(input, init).has('Authorization');
        const attemptedToken = token;
        const firstInit = mutating && !callerAuthorized
            ? withToken(input, init, attemptedToken) : init;
        const response = await originalFetch.call(this, input, firstInit);

        // Recovery is deliberately narrower than ordinary injection: never
        // refresh for GETs, external calls, form posts, or caller-owned auth.
        if (response.status !== 401 || !mutating || callerAuthorized
                || !isSameOriginApi(input)) {
            return response;
        }

        const fresh = await refreshToken();
        if (!fresh || fresh === attemptedToken) return response;
        return originalFetch.call(this, input, withToken(input, init, fresh));
    };
})();
