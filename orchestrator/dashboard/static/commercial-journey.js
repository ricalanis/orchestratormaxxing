/*
 * Hallmark component stamp
 * Direction: Hermes utilitarian-density; one continuous strategic rail.
 * Reuses the dashboard's zinc/blue/amber production tokens and typography.
 * Pre-emit critique: no decorative cards, fake score, gradients, or inferred calls.
 */
(function commercialJourneyFactory(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.CommercialJourney = api;
}(typeof window !== 'undefined' ? window : null, function buildCommercialJourney() {
    'use strict';

    const VERSIONED_STATES = new Set(['draft', 'verified', 'sent']);

    function list(value) {
        return Array.isArray(value) ? value : [];
    }

    function plural(count, singular, pluralForm) {
        return `${count} ${count === 1 ? singular : pluralForm}`;
    }

    function buildModel(payload) {
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
            throw new TypeError('Growth radar payload must be an object');
        }

        const rings = payload.rings && typeof payload.rings === 'object' ? payload.rings : {};
        const people = list(rings.seguimiento);
        const opportunities = list(rings.oportunidad);
        const proposalStage = list(rings.propuesta);
        const projects = list(payload.centro);
        const wonWithoutProject = list(payload.won_sin_proyecto);
        const versioned = proposalStage.filter(item => item && VERSIONED_STATES.has(item.proposal_state));
        const missingPacket = proposalStage.filter(item => !item || !VERSIONED_STATES.has(item.proposal_state));
        const drafts = proposalStage.filter(item => item && item.proposal_state === 'draft');
        const verified = proposalStage.filter(item => item && item.proposal_state === 'verified');
        const sent = proposalStage.filter(item => item && item.proposal_state === 'sent');

        let next;
        if (wonWithoutProject.length) {
            next = {
                key: 'register-project',
                text: `Registrar Project para ${plural(wonWithoutProject.length, 'deal ganado', 'deals ganados')}.`,
            };
        } else if (missingPacket.length) {
            next = {
                key: 'version-proposal',
                text: `Versionar ${plural(missingPacket.length, 'propuesta', 'propuestas')} desde su workspace.`,
            };
        } else if (drafts.length) {
            next = {
                key: 'verify-proposal',
                text: `Verificar calidad de ${plural(drafts.length, 'propuesta', 'propuestas')} antes del envío.`,
            };
        } else if (verified.length) {
            next = {
                key: 'human-send',
                text: `Hacer el envío humano de ${plural(verified.length, 'propuesta verificada', 'propuestas verificadas')}.`,
            };
        } else if (sent.length) {
            next = {
                key: 'follow-sent',
                text: `Dar seguimiento a ${plural(sent.length, 'propuesta enviada', 'propuestas enviadas')}.`,
            };
        } else if (opportunities.length) {
            next = {
                key: 'ground-opportunity',
                text: `Aterrizar ${plural(opportunities.length, 'oportunidad', 'oportunidades')} en workspace.`,
            };
        } else if (people.length) {
            next = {
                key: 'qualify-person',
                text: `Calificar ${plural(people.length, 'persona en seguimiento', 'personas en seguimiento')}.`,
            };
        } else {
            next = {
                key: 'start-followup',
                text: 'Iniciar seguimiento con una persona concreta.',
            };
        }

        return {
            callEvidence: 'optional',
            proposalStageCount: proposalStage.length,
            sentCount: sent.length,
            next,
            stages: [
                {
                    key: 'person',
                    label: 'Persona',
                    count: people.length,
                    gate: 'Seguimiento real + problema observado',
                },
                {
                    key: 'opportunity',
                    label: 'Oportunidad',
                    count: opportunities.length,
                    gate: 'Necesidad + valor + próxima decisión',
                },
                {
                    key: 'proposal',
                    label: 'Propuesta versionada',
                    count: versioned.length,
                    denominator: proposalStage.length,
                    gate: 'Workspace + alcance + aceptación + precio',
                },
                {
                    key: 'project',
                    label: 'Proyecto',
                    count: projects.length,
                    gate: 'Envío humano + ganado + repo_path',
                },
            ],
        };
    }

    function node(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }

    function render(container, payload) {
        if (!container) return;
        const model = buildModel(payload);
        container.replaceChildren();

        const section = node('section', 'bg-zinc-950 border border-zinc-800 rounded-xl overflow-hidden');
        section.dataset.testid = 'commercial-journey';
        section.setAttribute('aria-labelledby', 'commercial-journey-title');

        const heading = node('div', 'px-4 py-3 border-b border-zinc-800 flex items-start justify-between gap-4 flex-wrap');
        const intro = node('div');
        const title = node('h3', 'text-sm font-semibold text-zinc-100', 'De persona a proyecto');
        title.id = 'commercial-journey-title';
        intro.append(title, node('p', 'mt-0.5 text-[11px] text-zinc-500', 'La estrategia comercial, en una línea.'));
        heading.append(intro, node('p', 'text-[10px] text-zinc-500', 'Método: journey-first'));
        section.append(heading);

        const stages = node('ol', 'grid grid-cols-2 lg:grid-cols-4 gap-px bg-zinc-800');
        model.stages.forEach((stage, index) => {
            const item = node('li', 'bg-zinc-950 px-4 py-3 min-w-0');
            const top = node('div', 'flex items-baseline justify-between gap-3');
            top.append(
                node('span', 'text-[10px] font-mono text-zinc-500', `0${index + 1}`),
                node('strong', 'text-base font-semibold text-zinc-100', stage.denominator === undefined ? String(stage.count) : `${stage.count} de ${stage.denominator}`),
            );
            item.append(
                top,
                node('div', 'mt-1 text-xs font-medium text-zinc-200', stage.label),
                node('p', 'mt-1 text-[10px] leading-relaxed text-zinc-500', stage.gate),
            );
            stages.append(item);
        });
        section.append(stages);

        const footer = node('div', 'px-4 py-3 border-t border-zinc-800 flex items-start justify-between gap-4 flex-wrap');
        const now = node('p', 'text-xs text-zinc-300');
        now.append(node('strong', 'text-amber-400', 'Ahora: '), document.createTextNode(model.next.text));
        footer.append(now, node('p', 'text-[10px] text-zinc-500', 'La llamada puede ayudar; no es requisito.'));
        section.append(footer);
        container.append(section);
    }

    function renderLoading(container) {
        if (!container) return;
        container.replaceChildren(node('div', 'skeleton', ''));
        if (container.firstElementChild) container.firstElementChild.style.height = '10rem';
    }

    function renderError(container) {
        if (!container) return;
        container.replaceChildren();
        const section = node('section', 'border border-zinc-800 rounded-xl px-4 py-3');
        section.dataset.testid = 'commercial-journey';
        section.append(
            node('h3', 'text-sm font-semibold text-zinc-200', 'De persona a proyecto'),
            node('p', 'mt-1 text-xs text-zinc-500', 'Indicador no disponible. Los conteos no se sustituyeron por ceros.'),
        );
        container.append(section);
    }

    return { buildModel, render, renderLoading, renderError };
}));
