<script lang="ts">
	import type { BoardImage, Pass, Rating } from '$lib/api';
	import StageBadge from '$lib/StageBadge.svelte';
	import Thumb from '$lib/Thumb.svelte';
	import { lightbox, type LightboxItem } from '$lib/lightbox.svelte';
	import { when } from '$lib/format';

	let { data } = $props();
	const p = $derived(data.project);

	type Filter = 'all' | 'pick' | 'maybe' | 'unrated';
	let filter = $state<Filter>('all');
	let hideRejects = $state(true);

	function shown(im: BoardImage): boolean {
		if (hideRejects && im.rating === 'reject' && filter === 'all') return false;
		if (filter === 'all') return true;
		if (filter === 'unrated') return !im.rating;
		return im.rating === filter;
	}

	const passes = $derived(
		p.passes
			.map((pass) => ({ ...pass, visible: pass.images.filter(shown) }))
			.filter((pass) => pass.visible.length || (filter === 'all' && !hideRejects))
	);

	// every visible image, in page order, so the lightbox arrows walk the whole board
	const items = $derived(
		passes.flatMap((pass) =>
			pass.visible.filter((im) => im.url).map((im): LightboxItem => item(pass, im))
		)
	);

	function item(pass: Pass, im: BoardImage): LightboxItem {
		const notes = im.notes.map((n) => `${n.author}: ${n.text}`).join(' · ');
		return {
			url: im.url!,
			title: `${im.id}${im.rating ? ` · ${im.rating}` : ''} · seed ${im.seed ?? '?'}`,
			caption: notes || pass.text || pass.extra || im.prompt || pass.prompt || ''
		};
	}

	function open(pass: Pass, im: BoardImage) {
		lightbox.open(items, Math.max(0, items.findIndex((x) => x.url === im.url)));
	}

	const filters: { id: Filter; label: (c: typeof p.counts) => string }[] = [
		{ id: 'all', label: () => 'All' },
		{ id: 'pick', label: (c) => `Picks ${c.pick}` },
		{ id: 'maybe', label: (c) => `Maybe ${c.maybe}` },
		{ id: 'unrated', label: (c) => `Unrated ${c.unrated}` }
	];
	const rated = (r: Rating) => r ?? '';
</script>

<svelte:head><title>{p.title} · imagegen</title></svelte:head>

<div class="page">
	<header class="head">
		<div class="titles">
			<a href="/projects" class="small">Projects</a>
			<h1>{p.title} <span class="muted">v{p.version}</span></h1>
			{#if p.brief}<p class="muted">{p.brief}</p>{/if}
		</div>
		<div class="chips">
			{#if p.stage}<StageBadge stage={p.stage.stage} character={p.stage.character} />{/if}
			{#each p.state.cast as name (name)}
				<a class="chip" href="/characters/{encodeURIComponent(name)}">{name}</a>
			{/each}
			{#if p.state.preset}<span class="chip">{p.state.preset}</span>{/if}
		</div>
	</header>

	<div class="layout">
		<div class="main">
			{#if p.assets.length || p.state.refs.length}
				<section class="section">
					<h2>Refs and assets</h2>
					<div class="thumbs" style="--thumb: 140px">
						{#each p.state.refs as r (r.name)}
							<figure>
								<Thumb
									url={r.url}
									alt={r.name}
									label="ref"
									onclick={() => r.url && lightbox.open([{ url: r.url, title: `ref ${r.name}`, caption: r.role }])}
								/>
								<figcaption class="small">{r.name}{#if r.role}<span class="muted"> · {r.role}</span>{/if}</figcaption>
							</figure>
						{/each}
						{#each p.assets as a (a.name)}
							<figure>
								<Thumb
									url={a.url}
									alt={a.name}
									label="v{a.current}"
									onclick={() =>
										a.url && lightbox.open([{ url: a.url, title: `${a.name} v${a.current}`, caption: a.reasons.join('; ') }])}
								/>
								<figcaption class="small">
									{a.name}
									{#if a.stale}<span class="chip warn" title={a.reasons.join('\n')}>stale</span>{/if}
								</figcaption>
							</figure>
						{/each}
					</div>
				</section>
			{/if}

			<section class="section">
				<div class="section-head">
					<h2>Board</h2>
					<div class="filters">
						{#each filters as f (f.id)}
							<button class:on={filter === f.id} onclick={() => (filter = f.id)}>{f.label(p.counts)}</button>
						{/each}
						<label class="small muted"><input type="checkbox" bind:checked={hideRejects} /> hide rejects</label>
					</div>
				</div>
				{#each passes as pass (pass.n)}
					<article class="pass">
						<header>
							<strong>Pass {pass.n}</strong>
							<span class="muted small">v{pass.version} · {when(pass.at)} · {pass.author}</span>
							{#if pass.stage}<StageBadge stage={pass.stage} />{/if}
							{#if pass.sheet}<a class="small" href={pass.sheet} target="_blank" rel="noopener">contact sheet</a>{/if}
						</header>
						{#if pass.text || pass.extra}<p class="small pass-note">{pass.text ?? pass.extra}</p>{/if}
						{#if pass.missing}
							<p class="small muted">This pass's run folder is gone.</p>
						{/if}
						<div class="thumbs">
							{#each pass.visible as im (im.id)}
								<div class="cell {rated(im.rating)}">
									<Thumb url={im.url} alt={im.id} label={im.id} rating={im.rating} onclick={() => open(pass, im)} />
									{#each im.notes as n, i (i)}
										<p class="note small"><b>{n.author}</b> {n.text}</p>
									{/each}
								</div>
							{/each}
						</div>
					</article>
				{:else}
					<p class="empty">Nothing matches this filter.</p>
				{/each}
			</section>
		</div>

		<aside class="side">
			<section class="panel section">
				<h3>Prompt</h3>
				<pre class="prompt">{p.state.prompt || '(no prompt)'}</pre>
				{#if p.state.negative}
					<h3>Negative</h3>
					<pre class="prompt">{p.state.negative}</pre>
				{/if}
				{#if p.state.palette.length}
					<div class="swatches">
						{#each p.state.palette as col (col)}<span class="swatch" style:background={col} title={col}></span>{/each}
					</div>
				{/if}
				{#if p.state.docs.length}<p class="small muted">Docs: {p.state.docs.join(', ')}</p>{/if}
			</section>

			{#if p.notes.length}
				<section class="panel section">
					<h3>Board notes</h3>
					{#each [...p.notes].reverse() as n, i (i)}
						<p class="note small"><b>{n.author}</b> {n.text} <span class="muted">{when(n.at)}</span></p>
					{/each}
				</section>
			{/if}

			<section class="panel section">
				<h3>History</h3>
				<ol class="history">
					{#each p.versions as v (v.version)}
						<li>
							<strong>v{v.version}</strong>
							<span>{v.note || v.changed.join(', ') || 'created'}</span>
							<span class="muted small">{v.author} · {when(v.at)}</span>
						</li>
					{/each}
				</ol>
			</section>
		</aside>
	</div>
</div>

<style>
	.head {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: flex-end;
		gap: 12px;
	}
	.head p {
		margin: 4px 0 0;
	}
	.layout {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 340px;
		gap: 24px;
		align-items: start;
	}
	@media (max-width: 1000px) {
		.layout {
			grid-template-columns: 1fr;
		}
	}
	.main {
		display: flex;
		flex-direction: column;
		gap: 28px;
		min-width: 0;
	}
	.side {
		display: flex;
		flex-direction: column;
		gap: 14px;
		position: sticky;
		top: 70px;
	}
	figure {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	figcaption {
		display: flex;
		gap: 6px;
		align-items: center;
	}
	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
	}
	.filters button.on {
		border-color: var(--accent);
		color: var(--accent);
	}
	.pass {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding-top: 6px;
	}
	.pass header {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		align-items: center;
	}
	.pass-note {
		margin: 0;
		color: var(--violet);
	}
	.cell {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.note {
		margin: 0;
		color: var(--muted);
	}
	.note b {
		color: var(--text);
		font-weight: 600;
	}
	.history {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-height: 340px;
		overflow: auto;
	}
	.history li {
		display: flex;
		flex-direction: column;
	}
</style>
