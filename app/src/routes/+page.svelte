<script lang="ts">
	import ProjectTile from '$lib/ProjectTile.svelte';
	import CharacterTile from '$lib/CharacterTile.svelte';
	import RunCard from '$lib/RunCard.svelte';
	import { lightbox } from '$lib/lightbox.svelte';
	import { day, plural } from '$lib/format';

	let { data } = $props();
	const o = $derived(data.overview);
	const wall = $derived(o.wallpaper);
	const wallImage = $derived(wall?.images.find((im) => !im.blank && im.url) ?? null);
</script>

<svelte:head><title>imagegen</title></svelte:head>

<div class="page">
	{#if wall && wallImage}
		<button
			class="hero"
			style:background-image="url({JSON.stringify(wallImage.url)})"
			onclick={() =>
				lightbox.open([
					{ url: wallImage.url!, title: wall.daily?.theme ?? wall.prompt ?? '', href: `/runs/${wall.id}` }
				])}
		>
			<span class="hero-text">
				<span class="kicker">Latest wallpaper · {day(wall.daily?.date ?? wall.date)}</span>
				<strong>{wall.daily?.theme ?? wall.prompt}</strong>
				{#if wall.daily?.props?.length}<span class="small">with {wall.daily.props.join(', ')}</span>{/if}
			</span>
		</button>
	{/if}

	<p class="stats muted">
		{plural(o.projects, 'project')} · {plural(o.characters, 'character')} · {plural(o.runs, 'run')} ·
		{plural(o.images, 'image')}
	</p>

	<section class="section">
		<div class="section-head">
			<h2>Projects</h2>
			<a href="/projects" class="small">All projects</a>
		</div>
		{#if data.projects.length}
			<div class="cards">
				{#each data.projects.slice(0, 6) as p (p.slug)}<ProjectTile {p} />{/each}
			</div>
		{:else}
			<p class="empty">No projects yet. Start one with <code>gen project new SLUG --prompt "..."</code></p>
		{/if}
	</section>

	<section class="section">
		<div class="section-head">
			<h2>Characters</h2>
			<a href="/characters" class="small">All characters</a>
		</div>
		{#if data.characters.length}
			<div class="cards">
				{#each data.characters as c (c.name)}<CharacterTile {c} />{/each}
			</div>
		{:else}
			<p class="empty">No characters yet. <code>gen char new NAME --description "..."</code></p>
		{/if}
	</section>

	<section class="section">
		<div class="section-head">
			<h2>Recent runs</h2>
			<a href="/runs" class="small">All runs</a>
		</div>
		{#if o.recent.length}
			<div class="cards">
				{#each o.recent as run (run.id)}<RunCard {run} />{/each}
			</div>
		{:else}
			<p class="empty">Nothing generated yet.</p>
		{/if}
	</section>
</div>

<style>
	.hero {
		position: relative;
		display: block;
		width: 100%;
		aspect-ratio: 16 / 6;
		min-height: 220px;
		padding: 0;
		border-radius: 18px;
		border: 1px solid var(--line);
		background-size: cover;
		background-position: center;
		overflow: hidden;
		cursor: zoom-in;
		text-align: left;
	}
	.hero-text {
		position: absolute;
		inset: auto 0 0 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 48px 24px 20px;
		background: linear-gradient(transparent, rgb(8 6 14 / 0.9));
	}
	.hero-text strong {
		font-size: 22px;
	}
	.kicker {
		color: var(--accent);
		font-size: 12px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.06em;
	}
	.stats {
		margin: -12px 0 0;
		font-size: 14px;
	}
</style>
