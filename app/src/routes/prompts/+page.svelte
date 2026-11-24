<script lang="ts">
	import StageBadge from '$lib/StageBadge.svelte';
	import { day } from '$lib/format';
	import type { ThemeRow } from '$lib/api';

	let { data } = $props();
	let q = $state('');
	const has = (...texts: (string | null | undefined)[]) =>
		!q.trim() || texts.some((t) => t?.toLowerCase().includes(q.trim().toLowerCase()));

	const pool = $derived(data.themes.pool.filter((t) => has(t.text)));
	const special = $derived(
		(['dates', 'months', 'weekdays'] as const).flatMap((kind) =>
			Object.entries(data.themes[kind])
				.map(([key, rows]) => ({ kind, key, rows: rows.filter((t) => has(t.text, key)) }))
				.filter((g) => g.rows.length)
		)
	);
	const propGroups = $derived(
		Object.entries(data.props)
			.map(([cat, items]) => ({ cat, items: items.filter((i) => has(i, cat)) }))
			.filter((p) => p.items.length)
	);
	const styles = $derived(data.styles.filter((s) => has(s.name, s.description, s.suffix, s.negative_extra)));
	const presets = $derived(
		data.presets.filter((p) => has(p.name, p.description, p.model, p.suffix, p.negative_extra))
	);
	const characters = $derived(data.characters.filter((c) => has(c.name, c.title, c.description)));
	const projects = $derived(
		data.projects.filter((p) =>
			has(p.slug, p.title, p.prompt, ...Object.entries(p.snippets).flat())
		)
	);
	const unused = $derived(data.themes.pool.filter((t) => !t.uses).length);
	const nav = [
		['daily', 'Daily'],
		['themes', 'Themes'],
		['props', 'Props'],
		['styles', 'Styles'],
		['presets', 'Presets'],
		['templates', 'Templates'],
		['stages', 'Stages'],
		['characters', 'Characters'],
		['projects', 'Projects']
	];
</script>

<svelte:head><title>Prompts · imagegen</title></svelte:head>

{#snippet row(t: ThemeRow)}
	<li>
		<span class="text">{t.text}</span>
		<span class="meta small muted">
			{#if t.uses}used {t.uses}× · last {day(t.last ?? '')}{:else}not used yet{/if}
		</span>
	</li>
{/snippet}

<div class="page">
	<div class="section-head">
		<h1>Prompts</h1>
		<input type="search" placeholder="Filter everything…" bind:value={q} aria-label="Filter prompts" />
	</div>
	<p class="muted small lead">
		Everything that shapes a prompt, read from <code>config.toml</code>, the characters and the projects.
		Read-only: change things in those files for now.
	</p>
	{#if data.errors.length}
		<div class="panel warn">
			<strong>Some parts did not load:</strong>
			<ul>{#each data.errors as e (e)}<li>{e}</li>{/each}</ul>
		</div>
	{/if}
	<nav class="jump chips" aria-label="Sections">
		{#each nav as [id, label] (id)}<a class="chip" href="#{id}">{label}</a>{/each}
	</nav>

	<section id="daily" class="section">
		<h2>Daily wallpaper</h2>
		<div class="panel facts">
			<div><span class="muted">preset</span> <strong>{data.daily.preset}</strong></div>
			<div><span class="muted">style</span> <strong>{data.daily.style || 'the preset’s'}</strong></div>
			<div>
				<span class="muted">cast</span> <strong>{data.daily.cast.join(', ') || 'nobody'}</strong>
			</div>
			<div><span class="muted">no repeat for</span> <strong>{data.daily.no_repeat_days} days</strong></div>
			<div><span class="muted">props a day</span> <strong>{data.daily.props_per_day}</strong></div>
		</div>
	</section>

	<section id="themes" class="section">
		<div class="section-head">
			<h2>Themes</h2>
			<span class="muted small"
				>{data.themes.pool.length} in the pool · {unused} never used{#if q} · showing {pool.length}{/if}</span
			>
		</div>
		<details open>
			<summary>Pool ({pool.length})</summary>
			<ul class="rows">{#each pool as t (t.text)}{@render row(t)}{/each}</ul>
		</details>
		{#each special as g (g.kind + g.key)}
			<details>
				<summary>
					{g.kind === 'dates' ? 'Date' : g.kind === 'months' ? 'Month' : 'Weekday'}
					<strong>{g.key}</strong>
					<span class="muted small">({g.rows.length})</span>
				</summary>
				<ul class="rows">{#each g.rows as t (t.text)}{@render row(t)}{/each}</ul>
			</details>
		{/each}
	</section>

	<section id="props" class="section">
		<div class="section-head">
			<h2>Props</h2>
			<span class="muted small">{data.daily.props_per_day} a day, each from a different category</span>
		</div>
		<div class="cards">
			{#each propGroups as p (p.cat)}
				<div class="panel">
					<h3>{p.cat} <span class="muted small">{p.items.length}</span></h3>
					<ul class="plain">{#each p.items as i (i)}<li>{i}</li>{/each}</ul>
				</div>
			{/each}
		</div>
	</section>

	<section id="styles" class="section">
		<h2>Styles</h2>
		<div class="cards wide">
			{#each styles as s (s.name)}
				<div class="panel">
					<div class="row">
						<h3>{s.name}</h3>
						{#if s.daily}<span class="chip">daily</span>{/if}
						{#each s.post as step (step)}<span class="chip">post: {step}</span>{/each}
					</div>
					{#if s.description}<p class="small muted">{s.description}</p>{/if}
					<p class="prompt">{s.suffix}</p>
					{#if s.negative_extra}<p class="small muted">avoid: {s.negative_extra}</p>{/if}
					{#if s.presets.length}<p class="small muted">default for {s.presets.join(', ')}</p>{/if}
				</div>
			{/each}
		</div>
	</section>

	<section id="presets" class="section">
		<h2>Presets</h2>
		<ul class="rows presets">
			{#each presets as p (p.name)}
				<li>
					<div class="row">
						<strong>{p.name}</strong>
						{#if p.daily}<span class="chip">daily</span>{/if}
						{#if p.kind === 'edit'}<span class="chip">edit only</span>{/if}
						<span class="muted small">{[p.model, p.size].filter(Boolean).join(' · ')}</span>
					</div>
					<span class="small muted">{p.description}</span>
					{#if p.styles.length}<span class="small">styles: {p.styles.join(', ')}</span>{/if}
					{#if p.suffix}<span class="small">adds: <code>{p.suffix}</code></span>{/if}
					{#if p.negative_extra}<span class="small muted">avoid: <code>{p.negative_extra}</code></span>{/if}
				</li>
			{/each}
		</ul>
	</section>

	<section id="templates" class="section">
		<h2>Daily templates</h2>
		{#each [['Scene pass', data.templates.scene_prompt], ['Cast pass', data.templates.cast_prompt], ['Ref prompt', data.templates.ref_prompt]] as [label, t] (label)}
			{@const tpl = t as { text: string; custom: boolean }}
			<div class="panel">
				<div class="row">
					<h3>{label}</h3>
					<span class="chip">{tpl.custom ? 'from config.toml' : 'built in'}</span>
				</div>
				<p class="prompt">{tpl.text || '(empty)'}</p>
			</div>
		{/each}
		<div class="panel">
			<h3>Stand-ins</h3>
			<p class="small">default: {data.templates.stand_in}</p>
			{#each Object.entries(data.templates.stand_ins) as [who, text] (who)}
				<p class="small"><strong>{who}</strong>: {text}</p>
			{/each}
		</div>
	</section>

	<section id="stages" class="section">
		<h2>Character stages</h2>
		<div class="cards wide">
			{#each data.stages as s (s.stage)}
				<div class="panel">
					<div class="row">
						<StageBadge stage={s.stage} />
						<span class="muted small">{s.mode} · {s.preset} · {s.count} images{s.needs_text ? ' · needs text' : ''}</span>
					</div>
					{#if s.instruction}<p class="prompt">{s.instruction}</p>{/if}
					{#if s.ideas.length}<p class="small muted">ideas: {s.ideas.join(' · ')}</p>{/if}
				</div>
			{/each}
		</div>
	</section>

	<section id="characters" class="section">
		<h2>Characters</h2>
		<div class="cards wide">
			{#each characters as c (c.name)}
				<div class="panel">
					<div class="row">
						<a href="/characters/{encodeURIComponent(c.name)}"><h3>{c.title}</h3></a>
						<StageBadge stage={c.stage} />
					</div>
					<p class="prompt">{c.description || '(no description yet)'}</p>
					{#if c.projects.length}<p class="small muted">in {c.projects.join(', ')}</p>{/if}
				</div>
			{/each}
		</div>
	</section>

	<section id="projects" class="section">
		<h2>Projects</h2>
		<div class="cards wide">
			{#each projects as p (p.slug)}
				<div class="panel">
					<div class="row">
						<a href="/projects/{encodeURIComponent(p.slug)}"><h3>{p.title}</h3></a>
						{#if p.preset}<span class="chip">{p.preset}</span>{/if}
					</div>
					<p class="prompt">{p.prompt || '(no prompt yet)'}</p>
					{#each Object.entries(p.snippets) as [k, v] (k)}
						<p class="small"><strong>{k}</strong>: <span class="muted">{v}</span></p>
					{/each}
					{#if p.negative}<p class="small muted">avoid: {p.negative}</p>{/if}
					<p class="small muted">
						{p.cast.length ? `cast: ${p.cast.join(', ')}` : 'no cast'}{p.docs.length
							? ` · docs: ${p.docs.join(', ')}`
							: ''}
					</p>
				</div>
			{/each}
		</div>
	</section>
</div>

<style>
	.lead {
		margin: -12px 0 0;
	}
	.jump {
		position: sticky;
		top: 56px;
		z-index: 5;
		padding: 8px 0;
		background: var(--bg);
	}
	.jump a:hover {
		color: var(--text);
		text-decoration: none;
	}
	section {
		scroll-margin-top: 110px;
	}
	.panel.warn {
		border-color: var(--gold);
	}
	.facts {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 28px;
	}
	.row {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
	}
	.cards.wide {
		grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
	}
	.panel p {
		margin: 8px 0 0;
	}
	.rows,
	.plain {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.plain li {
		padding: 2px 0;
		font-size: 14px;
	}
	.rows li {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 8px 12px;
		border-bottom: 1px solid var(--line);
	}
	.presets li {
		gap: 4px;
	}
	details {
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		overflow: hidden;
	}
	summary {
		cursor: pointer;
		padding: 10px 14px;
	}
	code {
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--violet);
	}
	input[type='search'] {
		min-width: 220px;
	}
	@media (max-width: 640px) {
		.jump {
			top: 46px;
		}
		.cards.wide {
			grid-template-columns: 1fr;
		}
	}
</style>
