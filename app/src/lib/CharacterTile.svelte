<script lang="ts">
	import type { CharacterCard } from './api';
	import StageBadge from './StageBadge.svelte';

	let { c }: { c: CharacterCard } = $props();
</script>

<a class="tile" href="/characters/{encodeURIComponent(c.name)}">
	<div class="ref">
		{#if c.ref}<img src={c.ref} alt={c.title} loading="lazy" />{:else}<span class="muted small">no ref</span>{/if}
	</div>
	<div class="body">
		<div class="row">
			<strong>{c.title}</strong>
			<span class="muted small">v{c.version}</span>
		</div>
		<div><StageBadge stage={c.stage} /></div>
		{#if c.description}<p class="desc small muted">{c.description}</p>{/if}
		<div class="row">
			{#if c.palette.length}
				<span class="swatches">
					{#each c.palette as col (col)}<span class="swatch" style:background={col} title={col}></span>{/each}
				</span>
			{/if}
			{#if c.projects.length}<span class="small muted">in {c.projects.join(', ')}</span>{/if}
		</div>
	</div>
</a>

<style>
	.tile {
		display: grid;
		grid-template-columns: 120px 1fr;
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
	.ref {
		position: relative;
		overflow: hidden;
		background: var(--panel-2);
		display: grid;
		place-items: center;
		min-height: 140px;
	}
	.ref img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.body {
		padding: 12px 14px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
	}
	.row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 8px;
	}
	.desc {
		margin: 0;
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
</style>
