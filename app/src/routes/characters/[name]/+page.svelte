<script lang="ts">
	import type { Ref, StageName } from '$lib/api';
	import StageBadge from '$lib/StageBadge.svelte';
	import Thumb from '$lib/Thumb.svelte';
	import { lightbox } from '$lib/lightbox.svelte';
	import { when } from '$lib/format';

	let { data } = $props();
	const c = $derived(data.character);
	const STAGES: StageName[] = ['explore', 'converge', 'polish'];
	const at = $derived(STAGES.indexOf(c.stage));

	function show(refs: Ref[], i: number, v?: number) {
		const items = refs
			.filter((r) => r.url)
			.map((r) => ({ url: r.url!, title: `${r.name}${v ? ` · v${v}` : ''}`, caption: r.role }));
		lightbox.open(items, i);
	}
</script>

<svelte:head><title>{c.title} · imagegen</title></svelte:head>

<div class="page">
	<header class="head">
		<a href="/characters" class="small">Characters</a>
		<h1>{c.title} <span class="muted">v{c.version}</span></h1>
	</header>

	<div class="layout">
		<div class="refs">
			{#each c.refs as r, i (r.name)}
				<figure>
					<Thumb url={r.url} alt={r.name} onclick={() => show(c.refs, i)} />
					<figcaption class="small">{r.name}{#if r.role}<span class="muted"> · {r.role}</span>{/if}</figcaption>
				</figure>
			{:else}
				<p class="empty">No refs yet.</p>
			{/each}
		</div>

		<div class="info">
			<section class="panel section">
				<h3>Stage</h3>
				<ol class="stages" aria-label="Stages">
					{#each STAGES as s, i (s)}
						<li class:done={i < at} class:now={i === at}>
							{#if i === at}<StageBadge stage={s} />{:else}<span>{s}</span>{/if}
						</li>
					{/each}
				</ol>
			</section>
			<section class="panel section">
				<h3>Description</h3>
				<p class="desc">{c.description || '(none)'}</p>
				{#if c.palette.length}
					<div class="swatches">
						{#each c.palette as col (col)}<span class="swatch" style:background={col} title={col}></span>{/each}
					</div>
				{/if}
			</section>
			<section class="panel section">
				<h3>Used in</h3>
				{#each Object.entries(c.used_by) as [slug, u] (slug)}
					<p class="small">
						<a href="/projects/{encodeURIComponent(slug)}">{slug}</a>
						{#if u.pin}<span class="chip">pinned to v{u.pin}</span>{/if}
						{#if u.assets.length}<span class="muted"> · shows in {u.assets.join(', ')}</span>{/if}
					</p>
				{:else}
					<p class="small muted">No project casts {c.name} yet.</p>
				{/each}
			</section>
		</div>
	</div>

	<section class="section">
		<h2>History</h2>
		<ol class="timeline">
			{#each c.versions as v (v.version)}
				<li>
					<div class="when">
						<strong>v{v.version}</strong>
						<StageBadge stage={v.stage} />
						<span class="muted small">{v.author} · {when(v.at)}</span>
					</div>
					<p class="small">{v.note || v.changed.join(', ') || 'created'}</p>
					{#if v.refs.length}
						<div class="vrefs">
							{#each v.refs as r, i (r.name)}
								<Thumb url={r.url} alt="{r.name} v{v.version}" onclick={() => show(v.refs, i, v.version)} />
							{/each}
						</div>
					{/if}
				</li>
			{/each}
		</ol>
	</section>
</div>

<style>
	.head {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.layout {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 380px;
		gap: 24px;
		align-items: start;
	}
	@media (max-width: 900px) {
		.layout {
			grid-template-columns: 1fr;
		}
	}
	.refs {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
		gap: 12px;
	}
	figure {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.info {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}
	.desc {
		margin: 0;
		white-space: pre-wrap;
	}
	.stages {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		gap: 8px;
		align-items: center;
	}
	.stages li {
		text-transform: uppercase;
		font-size: 12px;
		letter-spacing: 0.04em;
		color: var(--muted);
	}
	.stages li.done {
		text-decoration: line-through;
	}
	.stages li:not(:last-child)::after {
		content: '→';
		margin-left: 8px;
		color: var(--line-strong);
	}
	.timeline {
		list-style: none;
		margin: 0;
		padding: 0 0 0 14px;
		border-left: 2px solid var(--line);
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.timeline li {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.timeline p {
		margin: 0;
	}
	.when {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		align-items: center;
	}
	.vrefs {
		display: grid;
		grid-template-columns: repeat(auto-fill, 96px);
		gap: 6px;
	}
</style>
