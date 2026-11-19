<script lang="ts">
	import type { ProjectCard } from './api';
	import StageBadge from './StageBadge.svelte';
	import { plural, when } from './format';

	let { p }: { p: ProjectCard } = $props();
</script>

<a class="tile" href="/projects/{encodeURIComponent(p.slug)}">
	<div class="cover">
		{#if p.cover}<img src={p.cover} alt="" loading="lazy" />{:else}<span class="muted small">no images yet</span>{/if}
	</div>
	<div class="body">
		<div class="row">
			<strong>{p.title}</strong>
			<span class="muted small">v{p.version}</span>
		</div>
		{#if p.stage}<div><StageBadge stage={p.stage.stage} character={p.stage.character} /></div>{/if}
		<div class="chips">
			<span class="chip">{plural(p.passes, 'pass')}</span>
			{#if p.counts.pick}<span class="chip pick">{p.counts.pick} picked</span>{/if}
			{#if p.counts.unrated}<span class="chip">{p.counts.unrated} unrated</span>{/if}
			{#if p.assets}<span class="chip">{p.assets} assets</span>{/if}
			{#if p.stale}<span class="chip warn">{p.stale} stale</span>{/if}
		</div>
		{#if p.updated}<span class="muted small">Updated {when(p.updated)}</span>{/if}
	</div>
</a>

<style>
	.tile {
		display: flex;
		flex-direction: column;
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: var(--radius);
		overflow: hidden;
		color: var(--text);
		transition:
			border-color 0.15s,
			transform 0.15s;
	}
	.tile:hover {
		border-color: var(--line-strong);
		transform: translateY(-2px);
		text-decoration: none;
	}
	.cover {
		position: relative;
		overflow: hidden;
		aspect-ratio: 16 / 9;
		background: var(--panel-2);
		display: grid;
		place-items: center;
	}
	.cover img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.body {
		padding: 12px 14px 14px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.row {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
	}
	.pick {
		color: var(--green);
	}
</style>
