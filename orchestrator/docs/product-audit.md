# Product Audit

## Summary
The dashboard's core operational loops (tasks, sessions, roadmap drilldown) are complete and work end-to-end. The CRM/growth layer is where journeys break: **contacts have no UI at all** (no list, no edit), **projects can't be edited**, and the deal model can't attach a product or an initiative from the UI despite the backend supporting both. Most "empty" areas (ICP, products, growth loops, scorecard = 0) are **data gaps**, not broken paths — the UIs exist and write correctly; they just have no data yet.

## Journey Coverage

| Journey | Works E2E? | Where it breaks |
|---|---|---|
| 1. Deal lifecycle (create → stages → win/lose) | ⚠️ | Stage moves + win/lose work via the edit-modal dropdown; but **no standalone "+ Deal"** (deals born only via `+Lead` or as sub-deals), **no product selector**, and **no initiative selector** in the deal modal (linkage is Hermes-MCP-only). |
| 2. Task lifecycle (create → assign → track → complete) | ✅ | Full path: `openNewTaskModal`→`POST /api/tasks`; assign via `PATCH .../assignee`; kanban tracking; status via sidecar `sprints.set_task_status`. |
| 3. Roadmap (view → drill → tasks) | ✅ | `GET /api/roadmap/{id}/drilldown` returns Initiative→Project→Epic→Task with per-epic progress bars, event history, and clickable task links. |
| 4. Sessions (view → find idle → act) | ✅ | Idle sessions grouped/collapsible; per-session modal supports send / revive / compact / kill. |
| 5. Scorecard (view → understand health) | ⚠️ | KPI tiles are labeled and auto-derived, but **no targets/benchmarks/derivation tooltip** → "is this healthy?" needs tribal knowledge. Values currently 0 (data gap). |
| 6. Lead capture / growth (+Lead → nurture → touch) | ⚠️ | Loop is closable: `+Lead`→deal, per-deal nurture generate, `+Touch` cadence with overdue flags. But growth-loop attribution is optional/mostly unset → loops all 0 (data gap). |
| 7. Contact & project editing | ❌ | **No contacts UI exists** (no list, no drawer field, no `update_contact` in `crm.py`, no PATCH route). **No project edit** (`PATCH /api/projects/{id}` absent). Note: initiative editing DOES work. |
| 8. Feature completeness vs roadmap / data quality | ⚠️ | ICP editor + products management ARE built (data empty = gap). Deal↔product and deal↔initiative relationships have **no UI write path** (broken, not empty). |

## Issues

### P0: Contacts are unmanageable from the UI
**Severity:** P0
**Location:** `dashboard/templates/index.html` (no `renderContacts`/`crm-contacts` element exists); `dashboard/crm.py:137` `create_contact`, `:301` `list_contacts` (no `update_contact`); `dashboard/api.py:2165` `GET /api/crm/contacts`, `:2172` `POST /api/crm/contacts` (no PATCH).
**Problem:** Contacts are created only as a side effect of `+Lead` (`/api/crm/leads`). There is no contacts list, no contact detail view, and no way to edit a contact. The backend has no `update_contact` function and no update route.
**Impact:** A core CRM journey (find a person → correct their email/company/role → follow up) is impossible. A typo captured at lead time is permanent from the UI; contact data can never be viewed as a list or corrected.
**Fix:** Add `crm.update_contact(...)` + `PATCH /api/crm/contacts/{id}`, and a Contacts sub-tab under CRM that renders `list_contacts()` with an inline edit form (mirror the deal-edit modal pattern at `index.html:4072`).

### P1: No standalone deal creation; deals can only be born from a lead
**Severity:** P1
**Location:** `dashboard/api.py:2194` `POST /api/crm/deals` exists but is unused by the UI; `index.html` has `openDealEdit` (edit-only, `:4072`) and no `+ Deal` button. Empty-state copy at `:4051` even tells the user to use `+ Lead` or Hermes.
**Problem:** The only UI paths to a new deal are `+Lead` (creates contact **and** deal) or adding a sub-deal to an existing deal. You cannot create a fresh deal for an **existing** account/contact without fabricating a new lead.
**Impact:** Blocks the common "existing client wants a second engagement" flow; forces duplicate contacts or a drop to the Hermes MCP.
**Fix:** Wire a `+ Deal` button to a create variant of the deal modal posting to `POST /api/crm/deals` (account picker already exists in the edit modal at `:4088`).

### P1: Deal modal has no product selector — deal↔product link unbuildable
**Severity:** P1
**Location:** Deal edit modal `dashboard/templates/index.html:760-807` (Title/Value/Stage/Account/Recurrence only); `dashboard/crm.py` `create_deal`/`update_deal` take **no** `product_id`; products management is fully built at `index.html:574` (`crm-products`), `renderProducts` `:4322`, `GET/POST /api/growth/products` `api.py:2529/2535`.
**Problem:** Products exist as a managed entity (list + create + edit + delete), but there is no field anywhere to attach a product to a deal, and the deal data model doesn't carry `product_id`.
**Impact:** The value-ladder / product-attribution that the growth model depends on can't be populated per deal → CLTV/CAC and per-product pipeline analytics can't be sliced by product.
**Fix:** Add `product_id` to `crm.create_deal`/`update_deal` + a product `<select>` in the deal modal (populate from `/api/growth/products`).

### P1: Deal↔initiative link is Hermes-only (no UI write path)
**Severity:** P1
**Location:** Deal drawer explicitly states it at `index.html:4939` ("the chain starts when Hermes links this deal to a quarterly bet (update_deal)"); `POST /api/crm/deals` and `PATCH /api/crm/deals/{id}` both accept `initiative_id` (`api.py:2201`, `:2226`) but the modal has no initiative field.
**Problem:** Deal cards render an initiative chip when linked, but there's no UI control to set/change that link — it only happens via the Hermes MCP `update_deal`.
**Impact:** Revenue→strategy attribution (deal → quarterly bet) can't be maintained by a dashboard user; the roadmap's deal-backed signal degrades to whatever the agents happened to set.
**Fix:** Add an initiative `<select>` to the deal modal (populate from `/api/roadmap`) posting `initiative_id`/`clear_initiative` (backend already supports both).

### P1: Projects can't be edited from the UI
**Severity:** P1
**Location:** `dashboard/api.py` has `POST /api/projects` (`:1722`), `POST /api/projects/{id}/archive` (`:804`), `GET /api/projects/{id}/detail` (`:1734`) — **no** `PATCH /api/projects/{id}`. Project detail view is read-only.
**Problem:** A project's name/description/metadata cannot be changed after creation; only create, archive, and per-task reassignment (`PATCH /api/tasks/{id}/project`) exist.
**Impact:** Journey 7 (edit a project description) has no path. A mistyped or evolving project has to be archived + recreated, orphaning its tasks. (Contrast: initiatives *are* fully editable via `PATCH /api/roadmap/{id}` incl. description — the asymmetry is the tell.)
**Fix:** Add `PATCH /api/projects/{id}` (title/description/status) + an edit control in the project detail drawer.

### P2: Scorecard isn't self-interpreting
**Severity:** P2
**Location:** `dashboard/growth.py:1033` `scorecard()` (5 KPI tiles); rendered from `/api/scorecard` at `index.html:3568`.
**Problem:** The weekly-5 tiles (leads/touches/discovery/content/proposals) show a raw count with an icon but no target, no prior-week delta, and no tooltip explaining they're auto-derived from `deal_events`. "3 touches" gives no sense of good/bad.
**Impact:** Pipeline health isn't interpretable without knowing the underlying model — a reporting gap, not a blocked action.
**Fix:** Add per-KPI target (from ICP cadence assumptions) + a WoW delta and a one-line "derived from events" tooltip.

### P2: CRM stage movement is dropdown-only (no board drag)
**Severity:** P2
**Location:** `crm-pipeline` is an 8-column stage board (`index.html:578`, filled at `:4056`) with `crmDealCard` (`:3991`); cards are not draggable (the `draggable`/`ondrop` handlers in the file are all for the task/cycle board, e.g. `:2204`, `:6082`).
**Problem:** Moving a deal between stages requires opening the edit modal and changing the Stage `<select>`; the visual pipeline board doesn't support drag-to-advance.
**Impact:** Minor friction; the journey completes, just slower than a kanban implies it should.
**Fix:** Add drag-to-stage on `crmDealCard` posting `PATCH /api/crm/deals/{id}` with the new stage (backend already logs `stage_changed`).

### P2: Empty data across ICP / products / growth loops / scorecard
**Severity:** P2 (data gap, NOT broken path)
**Location:** ICP editor modal (`index.html`, `PATCH /api/growth/icp` `api.py:2519`), products section, `GET /api/growth/loops` `:2358`, `/api/scorecard`.
**Problem:** These surfaces render "0"/empty because no ICP has been saved, no products created, no deals tagged with a growth loop, and no qualifying events this week. The write paths all work (verified: ICP `submitICP`, products `renderProducts`+POST).
**Impact:** The dashboard looks unpopulated/broken to a first-time user, but every one of these is a one-form-fill away from populated. Distinguish from the P0/P1 broken paths above.
**Fix:** Seed ICP + a product + tag existing deals with a growth loop; optionally add empty-state CTAs that deep-link to the relevant editor (the deals board already does this well at `:4051`).

## Top 3
1. **Contacts are invisible and uneditable (P0)** — add a Contacts view + `update_contact`/`PATCH` route. No CRM is complete when you can't see or fix a person.
2. **Deal model can't attach product or initiative from the UI (P1 ×2)** — add the two `<select>`s (backend already accepts both `product_id`-equivalent growth attrs and `initiative_id`); this is what makes revenue→product and revenue→strategy attribution real instead of agent-only.
3. **Projects can't be edited (P1)** — add `PATCH /api/projects/{id}` + an edit control, closing the read-only-project asymmetry against fully-editable initiatives.
