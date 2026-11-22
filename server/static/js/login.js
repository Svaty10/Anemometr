/* Přihlášení – bez inline skriptů (CSP). */
(function () {
    'use strict';
    const err = document.getElementById('err');
    const btn = document.getElementById('btn');
    document.getElementById('loginForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.classList.add('hidden');
        try {
            const r = await fetch('/api/login', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: document.getElementById('password').value })
            });
            let data = {};
            try { data = await r.json(); } catch (x) { /* ignore */ }
            if (!r.ok) throw new Error(data.error || ('Chyba ' + r.status));
            window.location.replace('/');
        } catch (x) {
            err.textContent = x.message || 'Chyba spojení se serverem.';
            err.classList.remove('hidden');
            document.getElementById('password').select();
        } finally {
            btn.disabled = false;
        }
    });
})();
