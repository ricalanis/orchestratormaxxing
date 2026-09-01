# Frontend Spec: Wellness Workspace

## File to modify
`orchestrator/dashboard/templates/index.html` (12K+ lines)

## What to add

### 1. Workspace nav button (after Knowledge, before Ops)
Insert after the `ws-knowledge` button (line ~248) and before `ws-ops` (line ~249):

```html
<button onclick="switchWorkspace('wellness')" id="ws-wellness"
        class="nav-tab px-5 py-3 text-sm font-medium flex items-center gap-2 text-zinc-400 hover:text-zinc-200">
    <span>🌿 Wellness</span>
    <span id="wellness-count" class="hidden px-1.5 py-px text-xs bg-emerald-600 text-white rounded-full font-semibold"></span>
</button>
```

### 2. Workspace content div
Insert a new `<div id="content-wellness" class="hidden">` after the existing `content-health` div (which ends at line ~1471) and before `content-archive`:

The content has three sub-views: Daily (timeline), Plate (nutrition), Supplements.

```html
<!-- ==================== WELLNESS (personal health) ==================== -->
<div id="content-wellness" class="hidden">
    <!-- Sub-view tabs -->
    <div class="flex items-center gap-1 mb-4">
        <button onclick="switchWellnessView('daily')" id="wv-daily"
                class="text-xs px-3 py-1 rounded-lg bg-zinc-800 text-zinc-100 font-medium">📅 Daily</button>
        <button onclick="switchWellnessView('plate')" id="wv-plate"
                class="text-xs px-3 py-1 rounded-lg text-zinc-400 hover:bg-zinc-800/60">🥗 Plate</button>
        <button onclick="switchWellnessView('supps')" id="wv-supps"
                class="text-xs px-3 py-1 rounded-lg text-zinc-400 hover:bg-zinc-800/60">💊 Supplements</button>
    </div>

    <!-- Daily Timeline view -->
    <div id="wellness-daily">
        <div class="flex items-center justify-between mb-4">
            <div>
                <h2 class="text-xl font-bold">🌿 Daily Ritual</h2>
                <p class="text-xs text-zinc-500 mt-0.5" id="wellness-date"></p>
            </div>
            <div class="flex items-center gap-3">
                <div class="text-right">
                    <div class="text-2xl font-bold" id="wellness-progress">0/12</div>
                    <div class="text-[11px] text-zinc-500">routines done</div>
                </div>
                <div class="text-right">
                    <div class="text-2xl font-bold text-amber-400" id="wellness-streak">0</div>
                    <div class="text-[11px] text-zinc-500">day streak</div>
                </div>
                <button onclick="loadWellness()" class="px-2.5 py-1.5 bg-zinc-800 hover:bg-zinc-700 rounded-lg text-sm" title="Refresh">🔄</button>
            </div>
        </div>
        <div id="wellness-timeline" class="space-y-6"></div>
    </div>

    <!-- Plate view (nutrition reference) -->
    <div id="wellness-plate" class="hidden">
        <div id="wellness-plate-content"></div>
    </div>

    <!-- Supplements view -->
    <div id="wellness-supps" class="hidden">
        <div id="wellness-supps-content"></div>
    </div>
</div>
```

### 3. JavaScript additions
Add these functions to the `<script>` section. Place them near the existing `loadHealth()` function (around line 2031).

```javascript
// ==================== WELLNESS (personal health) ====================
let WELLNESS_DATA = null;
let WELLNESS_PLATE = null;

async function loadWellness() {
    try {
        const d = await fetch('/api/health/today').then(r => r.json());
        WELLNESS_DATA = d;
        renderWellnessDaily(d);
    } catch (e) {
        console.error('wellness load error', e);
    }
}

function renderWellnessDaily(d) {
    const date = new Date(d.date + 'T00:00').toLocaleDateString('es-MX', {
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
    });
    document.getElementById('wellness-date').textContent = date;
    document.getElementById('wellness-progress').textContent = `${d.done}/${d.total}`;
    document.getElementById('wellness-streak').textContent = d.streak;

    const catColors = {
        exercise: 'emerald', devocional: 'purple', supplement: 'teal',
        meal: 'amber', meditation: 'violet', sleep: 'indigo'
    };
    const catBorder = {
        exercise: 'border-emerald-600', devocional: 'border-purple-600',
        supplement: 'border-teal-600', meal: 'border-amber-600',
        meditation: 'border-violet-600', sleep: 'border-indigo-600'
    };

    const html = d.blocks.map(block => {
        const items = block.items.map(r => {
            const colorCls = catColors[r.category] || 'zinc';
            const borderCls = catBorder[r.category] || 'border-zinc-700';
            const doneBtn = r.done
                ? `<button onclick="uncheckWellness(${r.id})" class="w-7 h-7 rounded-full bg-emerald-600 hover:bg-emerald-700 flex items-center justify-center text-white text-sm" title="Done — click to undo">✓</button>`
                : `<button onclick="checkWellness(${r.id})" class="w-7 h-7 rounded-full border-2 border-zinc-600 hover:border-zinc-400 flex items-center justify-center text-zinc-500 text-sm" title="Mark done">○</button>`;
            const linkBtn = r.link_url
                ? `<a href="${r.link_url}" target="_blank" rel="noopener" class="text-xs px-2 py-1 bg-zinc-800 hover:bg-zinc-700 rounded-lg text-blue-400 hover:text-blue-300 flex items-center gap-1">↗ ${r.link_label || 'Open'}</a>`
                : '';
            return `
                <div class="flex items-center gap-3 py-2 ${r.done ? 'opacity-50' : ''}">
                    ${doneBtn}
                    <span class="text-lg">${r.icon || '•'}</span>
                    <div class="flex-1 min-w-0">
                        <div class="flex items-center gap-2">
                            <span class="text-sm font-medium ${r.done ? 'line-through' : ''}">${r.label}</span>
                            ${r.target_time ? `<span class="text-[11px] text-zinc-500">${r.target_time}</span>` : ''}
                        </div>
                        ${r.description ? `<p class="text-xs text-zinc-500 mt-0.5">${r.description}</p>` : ''}
                    </div>
                    ${linkBtn}
                </div>`;
        }).join('');
        return `
            <div>
                <h3 class="text-sm font-semibold text-zinc-400 mb-1">${block.label}</h3>
                <div class="bg-zinc-900/50 border border-zinc-800 rounded-xl px-3 py-1 divide-y divide-zinc-800/50">${items}</div>
            </div>`;
    }).join('');
    document.getElementById('wellness-timeline').innerHTML = html;
}

async function checkWellness(id) {
    await fetch(`/api/health/routines/${id}/check`, { method: 'POST' });
    loadWellness();
}

async function uncheckWellness(id) {
    await fetch(`/api/health/routines/${id}/uncheck`, { method: 'POST' });
    loadWellness();
}

function switchWellnessView(view) {
    const views = ['daily', 'plate', 'supps'];
    views.forEach(v => {
        document.getElementById('wv-' + v).classList.toggle('bg-zinc-800', v === view);
        document.getElementById('wv-' + v).classList.toggle('text-zinc-100', v === view);
        document.getElementById('wv-' + v).classList.toggle('font-medium', v === view);
        document.getElementById('wv-' + v).classList.toggle('text-zinc-400', v !== view);
        document.getElementById('wellness-' + v).classList.toggle('hidden', v !== view);
    });
    if (view === 'plate' && !WELLNESS_PLATE) loadWellnessPlate();
    if (view === 'supps' && !WELLNESS_PLATE) loadWellnessPlate(); // supps data comes with plate
}

async function loadWellnessPlate() {
    try {
        const d = await fetch('/api/health/plate').then(r => r.json());
        WELLNESS_PLATE = d;
        renderWellnessPlate(d);
        renderWellnessSupps(d);
    } catch (e) {
        console.error('plate load error', e);
    }
}

function renderWellnessPlate(d) {
    const segColors = { verduras: 'emerald', proteina: 'teal', cereales: 'amber' };
    const segHtml = Object.entries(d.segments).map(([key, seg]) => {
        const color = segColors[key] || 'zinc';
        return `
            <div class="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                <div class="flex items-center gap-2 mb-2">
                    <span class="w-3 h-3 rounded bg-${color}-500"></span>
                    <h4 class="text-sm font-semibold">${seg.label}</h4>
                    <span class="text-xs text-zinc-500 ml-auto">${seg.portion}</span>
                </div>
                <p class="text-xs text-zinc-400 mb-2">${seg.measure}</p>
                <p class="text-xs text-zinc-500 mb-3">${seg.note}</p>
                <div class="flex flex-wrap gap-1.5">
                    ${seg.items.map(i => `<span class="text-xs px-2 py-1 bg-zinc-800 rounded-lg">${i}</span>`).join('')}
                </div>
            </div>`;
    }).join('');

    const psorHtml = `
        <div class="mt-6 bg-gradient-to-r from-purple-900/30 to-zinc-900 border border-purple-800/50 rounded-xl p-5">
            <h3 class="text-lg font-bold text-purple-300 mb-2">🧴 Psoriasis Layer</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3 mt-3">
                <div class="bg-zinc-900/60 border-l-4 border-emerald-600 rounded-lg p-3">
                    <h4 class="text-xs font-semibold text-emerald-400 uppercase mb-2">Agregar</h4>
                    <ul class="text-xs space-y-1">${d.psoriasis.add.map(i => `<li class="text-zinc-300">${i}</li>`).join('')}</ul>
                </div>
                <div class="bg-zinc-900/60 border-l-4 border-amber-600 rounded-lg p-3">
                    <h4 class="text-xs font-semibold text-amber-400 uppercase mb-2">Reducir</h4>
                    <ul class="text-xs space-y-1">${d.psoriasis.reduce.map(i => `<li class="text-zinc-300">${i}</li>`).join('')}</ul>
                </div>
                <div class="bg-zinc-900/60 border-l-4 border-red-600 rounded-lg p-3">
                    <h4 class="text-xs font-semibold text-red-400 uppercase mb-2">Evitar</h4>
                    <ul class="text-xs space-y-1">${d.psoriasis.avoid.map(i => `<li class="text-zinc-300">${i}</li>`).join('')}</ul>
                </div>
                <div class="bg-zinc-900/60 border-l-4 border-purple-600 rounded-lg p-3">
                    <h4 class="text-xs font-semibold text-purple-400 uppercase mb-2">Método</h4>
                    <ul class="text-xs space-y-1">${d.psoriasis.method.map(i => `<li class="text-zinc-300">${i}</li>`).join('')}</ul>
                </div>
            </div>
        </div>`;

    const macrosHtml = `
        <div class="flex gap-3 mb-4">
            <div class="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-2 text-center">
                <div class="text-lg font-bold text-amber-400">${d.macros.carbs}</div>
                <div class="text-[10px] uppercase text-zinc-500">Carbos</div>
            </div>
            <div class="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-2 text-center">
                <div class="text-lg font-bold text-teal-400">${d.macros.protein}</div>
                <div class="text-[10px] uppercase text-zinc-500">Proteína</div>
            </div>
            <div class="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-2 text-center">
                <div class="text-lg font-bold text-red-400">${d.macros.fat}</div>
                <div class="text-[10px] uppercase text-zinc-500">Grasa</div>
            </div>
            <div class="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-2 text-center">
                <div class="text-lg font-bold text-zinc-300">${d.calories.exercise}</div>
                <div class="text-[10px] uppercase text-zinc-500">kcal (ejercicio)</div>
            </div>
            <div class="bg-zinc-900 border border-zinc-800 rounded-xl px-4 py-2 text-center">
                <div class="text-lg font-bold text-zinc-300">${d.calories.no_exercise}</div>
                <div class="text-[10px] uppercase text-zinc-500">kcal (reposo)</div>
            </div>
        </div>`;

    document.getElementById('wellness-plate-content').innerHTML = `
        <h2 class="text-xl font-bold mb-2">🥗 Mi plato Balanced</h2>
        <p class="text-xs text-zinc-500 mb-4">Perfil mediterráneo · referencia para súper, plato, suplementos y piel</p>
        ${macrosHtml}
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">${segHtml}</div>
        ${psorHtml}
    `;
}

function renderWellnessSupps(d) {
    const html = d.supplements.map(s => `
        <div class="bg-zinc-900 border ${s.psor_note ? 'border-purple-800/50 bg-purple-900/10' : 'border-zinc-800'} rounded-xl p-4">
            <h4 class="text-sm font-semibold">${s.name}</h4>
            <div class="text-xs text-teal-400 font-medium mt-0.5">${s.dose}</div>
            <p class="text-xs text-zinc-500 mt-2">${s.note}</p>
            <span class="inline-block mt-2 text-[11px] bg-zinc-800 border border-zinc-700 rounded-full px-2 py-0.5 text-zinc-400">${s.when}</span>
        </div>
    `).join('');
    document.getElementById('wellness-supps-content').innerHTML = `
        <h2 class="text-xl font-bold mb-2">💊 Tu stack diario</h2>
        <p class="text-xs text-zinc-500 mb-4">Según tu reporte genético. La marca y dosis exacta las define tu equipo de salud.</p>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">${html}</div>
    `;
}
```

### 4. Wiring into the workspace system
In the `WS_SUBS` object (around line 2098), add:
```javascript
wellness:   [['daily', 'Daily'], ['plate', 'Plate'], ['supps', 'Supplements']],
```

In the `TAB_WORKSPACE` object (around line 2106), add:
```javascript
daily: 'wellness', plate: 'wellness', supps: 'wellness',
```

In the `ROUTE_TABS` array (around line 2157), add: `'daily', 'plate', 'supps'`

In the `switchTab` function (around line 2189), add to the tabs array: `'daily', 'plate', 'supps'`

In the `switchTab` function's loading section (around line 2249), add:
```javascript
if (tab === 'daily') {
    loadWellness();
}
```

Also need to update the `showTab`/`hideTab` logic — every content div is hidden by default with `class="hidden"`, and `switchTab` removes the `hidden` class from the active content div. So add `content-wellness` to that pattern. Look for where `content-` divs are toggled.

## Key patterns to follow
- All content divs use `id="content-<tab>"` and `class="hidden"`. The `switchTab` function toggles `hidden` on the right one.
- Dark theme: zinc-900 backgrounds, zinc-800 borders, zinc-400/500 text. Use Tailwind classes.
- API calls use `fetch('/api/...').then(r => r.json())`.
- The existing system health tab is `content-health` (Ops sub-view). The new personal health uses `content-wellness` to avoid collision.
- The `switchWorkspace` function calls `switchTab` which handles showing the content div.
- Each tab has a corresponding content div with `id="content-<tab>"`.

## Acceptance contract
1. The `🌿 Wellness` button appears in the workspace nav between Knowledge and Ops
2. Clicking it shows the daily timeline with 12 routines grouped by time block
3. Each routine has a check-off button that works (POST to API, re-render)
4. The Plate sub-view shows the Balanced plate segments, macros, calories, and psoriasis layer
5. The Supplements sub-view shows the supplement stack with doses and notes
6. No existing functionality breaks (system health tab still works under Ops)