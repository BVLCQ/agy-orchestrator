// agy-orchestrator — desktop statusbar chip + pane for the Antigravity (agy) offload lane.
// Data: GET /api/plugins/agy-orchestrator/state (served by the agy-orchestrator Python plugin).
// Picker (ADR-002): GET/POST /api/plugins/agy-orchestrator/routes + POST /models/refresh.
// Pane layout (2026-11-16): routes picker FIRST, pools as status cards with
// Google-sourced reset clocks (parsed from 429 "Resets in Xh Ym"), tier probe
// condensed to one line — raw JSON never rendered.
import { jsx } from 'react/jsx-runtime'
import { useState } from 'react'
import { useQuery, useQueryClient } from '@hermes/plugin-sdk'

const fmtTok = (n) => {
  if (n == null) return '–'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k'
  return String(n)
}

// All backend calls go through ctx.rest — the desktop bridge (auth + profile
// scoping + the /api/plugins/agy-orchestrator namespace). A raw fetch() cannot reach
// the gateway from the pane webview. PluginRestOptions supports method + body
// (verified: apps/desktop/src/api/plugins.ts).
const unwrap = (s) => {
  if (s && typeof s.json === 'function') return s.json()
  if (typeof s === 'string') { try { return JSON.parse(s) } catch (e) { return null } }
  return s
}

const ROUTE_ORDER = ['plan', 'parallel']

// "Claude Opus 5.5 (High)" -> "Claude Opus 5.5" — the effort rides in the
// option label itself so a CLOSED select still says which model is picked.
const baseName = (b) => String((b && b.label) || (b && b.base) || '')
  .replace(/\s*\([^)]*\)\s*$/, '')

const sectionTitle = {
  fontSize: '10px',
  textTransform: 'uppercase',
  letterSpacing: '0.08em',
  color: 'var(--ui-text-quaternary)',
  margin: '14px 0 5px 0',
}
const row = {
  display: 'flex',
  justifyContent: 'space-between',
  gap: '8px',
  fontSize: '11px',
  padding: '2px 0',
}
const label = { color: 'var(--ui-text-secondary)' }
const value = { color: 'var(--ui-text-secondary)', fontVariantNumeric: 'tabular-nums' }
const card = {
  background: 'var(--ui-card)',
  border: '1px solid var(--ui-stroke-secondary)',
  borderRadius: '6px',
  padding: '7px 9px',
  marginBottom: '6px',
}
const dim = { fontSize: '10px', color: 'var(--ui-text-quaternary)' }
const link = { color: 'var(--ui-accent)', cursor: 'pointer', userSelect: 'none' }

// Google emits "Resets in Xh Ym" inside 429 errors; the meter stores the
// horizon + observation time. Tick it down live from the observation anchor.
const resetRemainMin = (p) => {
  if (!p || p.quota_reset_min == null) return null
  let minsAgo = 0
  if (p.quota_reset_observed_at) {
    const t = Date.parse(p.quota_reset_observed_at)
    if (!Number.isNaN(t)) minsAgo = Math.floor((Date.now() - t) / 60000)
  }
  return Math.max(0, p.quota_reset_min - minsAgo)
}
const fmtDur = (min) => {
  const d = Math.floor(min / 1440), h = Math.floor((min % 1440) / 60), m = min % 60
  if (d > 0) return d + 'd ' + h + 'h'
  if (h > 0) return h + 'h' + (m ? ' ' + m + 'm' : '')
  return m + 'm'
}

export default {
  id: 'agy-orchestrator',
  name: 'AGY Orchestrator',
  register(ctx) {
    function useAgyState() {
      return useQuery({
        queryKey: ['agy-orchestrator-state'],
        queryFn: async () => {
          const s = await ctx.rest('/state', { method: 'GET', timeoutMs: 15000 })
          if (s && typeof s.json === 'function') return await s.json()
          if (typeof s === 'string') {
            try { return JSON.parse(s) } catch (e) { return { parse_error: String(s).slice(0, 120) } }
          }
          return s
        },
        refetchInterval: 30000,
        retry: 1,
      })
    }

    function Chip() {
      const { data } = useAgyState()
      const qc = useQueryClient()
      const auth = (data && data.auth) || {}
      const pools = (data && data.pools) || {}
      const g = pools.gemini || {}
      const t = pools.third_party || {}
      const mins = auth.minutes_left
      const warn = auth.needs_reauth
      const a = pools.argon || {}
      const burn = (p) => fmtTok((p && (p.week_tokens || ((p.tokens_in || 0) + (p.tokens_out || 0) + (p.thinking_tokens || 0)))) || 0)
      const stateTag = (p) => {
        if (!p) return ''
        if (p.dry) return '!dry'
        if (p.hot) return '!'
        return ''
      }
      const title = 'AGY meter — auth ' +
        (auth.ok == null ? '…' : auth.ok ? (mins != null ? mins + 'm left' : 'ok') : 'down') +
        ' · burn this week: gem ' + burn(g) + ' / argon ' + burn(a) + ' / 3rd ' + burn(t) +
        ' (exact, Hermes-dispatched) · ! = under pressure, !dry = bucket dry (calibrated)' +
        (warn ? ' · CLICK to attempt silent re-auth' : ' · click to refresh')
      const seg = (children, opts, key) => jsx('span', {
        key,
        children,
        style: { color: 'var(--ui-text-secondary)', ...(opts || {}) },
      })
      const onChipClick = async () => {
        try {
          if (warn) await ctx.rest('/reauth', { method: 'POST', timeoutMs: 60000 })
        } catch (e) { /* toast-free fallback: state refreshes on next poll */ }
        qc.invalidateQueries({ queryKey: ['agy-orchestrator-state'] })
      }
      return jsx('div', {
        title,
        onClick: onChipClick,
        style: { display: 'flex', gap: '7px', alignItems: 'center',
                 fontSize: '11px', opacity: data ? 1 : 0.55, cursor: 'pointer' },
        children: [
          seg('AGY', { fontWeight: 600, color: 'var(--ui-text-secondary)' }, 'label'),
          seg('g' + burn(g) + stateTag(g), stateTag(g) ? { color: 'var(--ui-accent)', fontWeight: 700 } : {}, 'g'),
          seg('a' + burn(a) + stateTag(a), stateTag(a) ? { color: 'var(--ui-accent)', fontWeight: 700 } : {}, 'a'),
          seg('3rd' + burn(t) + stateTag(t), stateTag(t) ? { color: 'var(--ui-accent)', fontWeight: 700 } : {}, 't'),
          seg(mins != null ? (warn ? 'reauth!' : mins + 'm') : '–',
              warn ? { color: 'var(--ui-accent)', fontWeight: 700 } : {}, 'auth'),
        ],
      })
    }

    function RoutesPicker() {
      const qc = useQueryClient()
      const { data, error } = useQuery({
        queryKey: ['agy-routes'],
        queryFn: async () =>
          unwrap(await ctx.rest('/routes', { method: 'GET', timeoutMs: 15000 })),
        refetchInterval: 60000,
        retry: 1,
      })
      const [slots, setSlots] = useState(null)
      const [status, setStatus] = useState(null)
      const routes = (data && data.routes) || {}
      const models = (data && data.models) || {}
      const bases = models.bases || []
      const base = slots || {
        plan: (routes.plan || []).slice(),
        parallel: (routes.parallel || []).slice(),
      }
      const slotsFor = (r) => {
        const list = [...(base[r] || [])]
        while (list.length < 2) list.push('')
        return list
      }
      const setSlot = (r, i, v) => {
        const list = slotsFor(r)
        list[i] = v || ''
        while (list.length > 2 && list[list.length - 1] === '') list.pop()
        setSlots({ ...base, [r]: list })
      }
      const inputStyle = {
        flex: 1, minWidth: 0, fontSize: '10px', padding: '3px 6px',
        color: 'var(--ui-text-secondary)',
        background: 'var(--ui-card)',
        border: '1px solid var(--ui-stroke-secondary)',
        borderRadius: '4px',
      }
      const selectEl = (r, i, list) => jsx('select', {
        key: r + '-' + i,
        value: list[i] || '',
        onChange: (e) => setSlot(r, i, e.target.value),
        style: inputStyle,
        children: [
          jsx('option', { key: 'none', value: '',
            children: i === 0 ? '— pick model —' : '— none —' }),
          ...bases.map((b) => jsx('optgroup', {
            key: b.base,
            label: baseName(b) + ' (' + (b.pool || '') + ')',
            children: (b.variants || []).map((v) => jsx('option', {
              key: v.slug, value: v.slug,
              children: v.effort ? baseName(b) + ' · ' + v.effort : baseName(b) })) })),
        ],
      })
      const canSave = ROUTE_ORDER.every((r) => slotsFor(r)[0])
      const dirty = slots != null
      const save = async () => {
        if (!canSave) return
        setStatus('saving…')
        try {
          const payload = { routes: {} }
          for (const r of ROUTE_ORDER) payload.routes[r] = slotsFor(r).filter(Boolean)
          await ctx.rest('/routes', {
            method: 'POST', body: payload, timeoutMs: 30000 })
          setStatus('saved ✓')
          setSlots(null)
          qc.invalidateQueries({ queryKey: ['agy-routes'] })
          qc.invalidateQueries({ queryKey: ['agy-orchestrator-state'] })
        } catch (e) { setStatus('error: ' + e.message) }
      }
      const refreshModels = async () => {
        setStatus('refreshing catalog…')
        try {
          await ctx.rest('/models/refresh', { method: 'POST', timeoutMs: 90000 })
          setStatus('catalog refreshed ✓')
          qc.invalidateQueries({ queryKey: ['agy-routes'] })
        } catch (e) { setStatus('error: ' + e.message) }
      }
      if (error && !data) {
        return jsx('div', {
          style: { fontSize: '10px', color: 'var(--ui-accent)', margin: '6px 0' },
          children: 'routes (model picker) — backend unreachable (' +
            String((error && error.message) || error).slice(0, 90) +
            '). If this persists, the gateway needs a restart: ' +
            '`hermes gateway restart` in Terminal.app.' })
      }
      if (!data) {
        return jsx('div', { style: { ...sectionTitle, textTransform: 'none' },
          children: 'routes (model picker) — loading…' })
      }
      return jsx('div', { children: [
        jsx('div', { style: { ...sectionTitle, margin: '0 0 5px 0' }, key: 't',
          children: 'routes — pick the models (primary → fallback)' }),
        ...ROUTE_ORDER.map((r) => {
          const list = slotsFor(r)
          return jsx('div', { key: 'route-' + r, style: card, children: [
            jsx('div', { key: 'lbl', style: { ...dim, textTransform: 'uppercase',
              letterSpacing: '0.06em', marginBottom: '4px' }, children: r }),
            jsx('div', { key: 'sel',
              style: { display: 'flex', gap: '4px', alignItems: 'center' },
              children: list.map((_, i) => [
                i > 0 ? jsx('span', { key: 'arr' + i,
                  style: { color: 'var(--ui-text-quaternary)' }, children: '→' }) : null,
                selectEl(r, i, list),
              ]).flat() }),
          ] })
        }),
        jsx('div', { key: 'delegate', style: { ...row, marginBottom: '4px' }, children: [
          jsx('span', { style: dim, children: 'hermes_delegate' }),
          jsx('span', { style: dim, children: 'delegate_task · glm-5.3-flash · fixed' }),
        ] }),
        jsx('div', { key: 'actions', style: { display: 'flex', gap: '12px',
          alignItems: 'center', margin: '2px 0' }, children: [
          jsx('span', { key: 'save', onClick: save,
            style: { ...link, fontSize: '11px', opacity: canSave ? 1 : 0.4 },
            children: (dirty ? '• ' : '') + 'save selection' }),
          jsx('span', { key: 'refresh', onClick: refreshModels,
            style: { ...link, fontSize: '11px' },
            children: '⟳ refresh catalog' }),
          status ? jsx('span', { key: 'status', style: dim,
            children: status }) : null,
        ] }),
        jsx('div', { key: 'cat', style: dim,
          children: bases.length + ' models · fetched '
            + String(models.fetched_at || '–').slice(5, 16)
            + ' · auto 6h' }),
      ] })
    }

    function Pane() {
      const { data, error } = useAgyState()
      const qc = useQueryClient()
      const triggerReauth = async () => {
        try { await ctx.rest('/reauth', { method: 'POST', timeoutMs: 60000 }) } catch (e) {}
        qc.invalidateQueries({ queryKey: ['agy-orchestrator-state'] })
      }
      if (error && !data) {
        return jsx('div', { style: { padding: '12px', fontSize: '12px',
                                     color: 'var(--ui-text-secondary)' },
          children: 'agy-orchestrator backend unreachable — is the agy-orchestrator plugin enabled?' })
      }
      if (!data) {
        return jsx('div', { style: { padding: '12px', fontSize: '12px',
                                     color: 'var(--ui-text-quaternary)' },
          children: 'loading…' })
      }
      const auth = data.auth || {}
      const quota = data.quota || {}
      const pools = data.pools || {}
      const glm = data.glm || {}
      const errs = (data.errors || []).slice(-5).reverse()
      const tier0 = ((quota.data && quota.data.allowedTiers) || [])[0] || {}
      const poolCard = (name, p) => {
        p = p || {}
        const dry = !!p.dry, hot = !!p.hot
        const dotColor = dry ? '#f85149' : hot ? '#d29922' : '#3fb950'
        const tag = dry ? 'dry — waiting on quota reset' : hot ? 'hot — router avoiding' : 'ok'
        const remain = resetRemainMin(p)
        return jsx('div', { style: card, children: [
          jsx('div', { key: 'head', style: { display: 'flex', alignItems: 'center', gap: '6px' }, children: [
            jsx('span', { key: 'dot', style: { width: '6px', height: '6px', borderRadius: '50%',
              background: dotColor, flexShrink: 0 } }),
            jsx('span', { key: 'nm', style: { ...label, fontWeight: 600 }, children: name }),
            jsx('span', { key: 'tag', style: { marginLeft: 'auto', ...value,
              color: (dry || hot) ? dotColor : 'var(--ui-text-quaternary)' },
              children: tag }),
          ] }),
          jsx('div', { key: 'stats', style: row, children: [
            jsx('span', { style: dim, children: 'week ' + fmtTok(p.week_tokens) +
              ' · ' + (p.calls || 0) + ' calls / ' + (p.errors || 0) + ' errors' +
              (p.budget_calibrated ? ' · budget calibrated' : '') }),
          ] }),
          jsx('div', { key: 'win', style: row, children: [
            jsx('span', { style: dim, children: '5h window in/out/think: ' +
              fmtTok(p.tokens_in) + ' / ' + fmtTok(p.tokens_out) + ' / ' + fmtTok(p.thinking_tokens) }),
          ] }),
          remain != null ? jsx('div', { key: 'reset', style: {
            ...dim, color: 'var(--ui-accent)', paddingTop: '2px' },
            title: 'horizon from Google\'s 429 message, observed ' +
              String(p.quota_reset_observed_at || '').slice(5, 16) +
              ' — ticks down live, not a Google API clock',
            children: '⏳ resets in ~' + fmtDur(remain) +
              ' (from 429 @ ' + String(p.quota_reset_observed_at || '').slice(5, 16) + ')' }) : null,
        ] })
      }
      return jsx('div', { style: { padding: '10px 12px', fontSize: '11px' }, children: [
        // Routes picker (ADR-002) — owner selects the models per route. FIRST.
        jsx(RoutesPicker, { key: 'routes-picker' }),
        // Auth — one compact row with inline action
        jsx('div', { style: sectionTitle, key: 'auth-t', children: 'auth' }),
        jsx('div', { style: row, key: 'auth-row', children: [
          jsx('span', { style: label, children: auth.email || '–' }),
          jsx('span', { style: { ...value, display: 'flex', gap: '8px', alignItems: 'center' }, children: [
            jsx('span', { key: 'st', style: auth.needs_reauth ? { color: '#d29922' } : {},
              children: auth.needs_reauth ? 'RE-AUTH NEEDED'
                : auth.minutes_left != null ? auth.minutes_left + ' min left' : (auth.ok ? 'ok' : 'down') }),
            jsx('span', { key: 'act', onClick: triggerReauth, style: link, children: '⟳' }),
          ] }),
        ] }),
        // Pools — status cards with Google-sourced reset clocks
        jsx('div', { style: sectionTitle, key: 'pools-t', children: 'quota pools' }),
        poolCard('gemini · shared (flash, hydrogen)', pools.gemini),
        poolCard('argon · dedicated', pools.argon),
        poolCard('third-party (opus, sonnet)', pools.third_party),
        jsx('div', { key: 'pools-note', style: { ...dim, margin: '2px 0 0 0' },
          children: 'burn is exact (Hermes-dispatched runs only) · IDE Usage panel = account truth' }),
        // Model usage (per-slug lifetime stats from the meter, ADR-003)
        (() => {
          const slugs = Object.entries(data.slugs || {})
            .sort((a, b) => ((b[1] && b[1].tokens) || 0) - ((a[1] && a[1].tokens) || 0))
            .slice(0, 6)
          if (!slugs.length) return null
          return jsx('div', { key: 'slug-usage', children: [
            jsx('div', { style: sectionTitle, children: 'model usage (lifetime)' }),
            ...slugs.map(([slug, s]) => jsx('div', { style: { ...row,
              borderBottom: '1px solid var(--ui-stroke-secondary)' }, key: 'slug-' + slug, children: [
              jsx('span', { style: label, children: slug }),
              jsx('span', { style: value,
                children: fmtTok(s.tokens) + ' tok · ' + (s.calls || 0) + ' calls' +
                          ((s.errors || 0) ? ' · ' + s.errors + ' ERR' : '') }),
            ] })),
          ] })
        })(),
        // Quota tier probe — one line; the raw JSON never rendered
        jsx('div', { style: sectionTitle, key: 'q-t', children: 'server quota (loadCodeAssist)' }),
        jsx('div', { style: row, key: 'q-row', children: [
          jsx('span', { style: label,
            children: (tier0.name || 'tier') + (tier0.id ? ' · ' + tier0.id : '') }),
          jsx('span', { style: value,
            children: 'http ' + (quota.http || '–') + ' · ' + (quota.fetched_at || '–').slice(5, 16) }),
        ] }),
        // GLM telemetry
        jsx('div', { style: sectionTitle, key: 'glm-t', children: 'hermes lanes (t/s ewma)' }),
        Object.keys(glm).length === 0 ? jsx('div', { style: dim, key: 'glm-empty', children: 'no data yet' }) :
        Object.entries(glm).map(([k, v]) => jsx('div', { style: row, key: 'glm-' + k, children: [
          jsx('span', { style: label, children: k }),
          jsx('span', { style: value,
            children: (v.tps_ewma != null ? v.tps_ewma + ' t/s · ' : '') + (v.calls || 0) + ' calls' }),
        ] })),
        // Errors
        errs.length ? jsx('div', { style: sectionTitle, key: 'err-t', children: 'recent errors' }) : null,
        ...errs.map((e, i) => jsx('div', { style: row, key: 'err-' + i, children: [
          jsx('span', { style: label, children: (e.ts || '').slice(5, 16) + ' ' + (e.provider || '') + '/' + (e.model || '') }),
          jsx('span', { style: value, children: String(e.status) }),
        ] })),
        jsx('div', { style: { ...row, marginTop: '4px' }, key: 'upd', children: [
          jsx('span', { style: dim, children: 'updated' }),
          jsx('span', { style: dim, children: (data.updated_at || '–').slice(5, 19) }),
        ] }),
      ] })
    }

    ctx.register({
      id: 'agy-orchestrator-chip',
      area: 'statusBar.right',
      render: () => jsx(Chip, {}),
    })

    ctx.register({
      id: 'agy-orchestrator-pane',
      area: 'panes',
      data: { placement: 'right', width: 340, title: 'AGY Orchestrator' },
      render: () => jsx(Pane, {}),
    })
  },
}
