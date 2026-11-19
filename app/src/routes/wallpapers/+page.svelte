<script lang="ts">
	import type { Run } from '$lib/api';
	import { lightbox, type LightboxItem } from '$lib/lightbox.svelte';
	import { day, when } from '$lib/format';

	let { data } = $props();
	let dailyOnly = $state(false);
	let showBlank = $state(false);
	const runs = $derived(data.runs.filter((r) => !dailyOnly || r.daily));

	const tiles = $derived(
		runs.flatMap((run) =>
			run.images
				.filter((im) => im.url && (showBlank || !im.blank))
				.map((im, i) => ({ run, im, first: i === 0 }))
		)
	);
	const items = $derived(tiles.map(({ run, im }): LightboxItem => itemFor(run, im.url!, im.seed)));

	function itemFor(run: Run, url: string, seed: number | null): LightboxItem {
		const props = run.daily?.props?.length ? ` · with ${run.daily.props.join(', ')}` : '';
		return {
			url,
			title: `${run.daily ? `Daily ${run.daily.date ?? run.date}` : run.date} · ${run.preset} · seed ${seed ?? '?'}`,
			caption: `${run.daily?.theme ?? run.prompt ?? ''}${props}`,
			href: `/runs/${run.id}`
		};
	}
</script>

<svelte:head><title>Wallpapers · imagegen</title></svelte:head>

<div class="page">
	<div class="section-head">
		<h1>Wallpapers</h1>
		<div class="filters small muted">
			<label><input type="checkbox" bind:checked={dailyOnly} /> daily only</label>
			<label><input type="checkbox" bind:checked={showBlank} /> show blank</label>
		</div>
	</div>

	{#if tiles.length}
		<div class="walls">
			{#each tiles as t, i (t.im.url)}
				<button class="wall" onclick={() => lightbox.open(items, i)}>
					<img src={t.im.url} alt={t.run.daily?.theme ?? t.run.prompt ?? ''} loading="lazy" />
					<span class="cap">
						{#if t.run.daily}<span class="kicker">Daily · {day(t.run.daily.date ?? t.run.date)}</span>
						{:else}<span class="kicker muted">{day(t.run.date)} · {t.run.preset}</span>{/if}
						<span class="theme">{t.run.daily?.theme ?? t.run.prompt}</span>
					</span>
					{#if t.im.blank}<span class="chip warn blank">blank</span>{/if}
				</button>
			{/each}
		</div>
	{:else}
		<p class="empty">No wallpapers yet. <code>gen daily</code> makes today's.</p>
	{/if}

	{#if data.log.length}
		<section class="panel section">
			<h2>Daily log</h2>
			<ul class="log">
				{#each data.log as l, i (i)}
					<li class:ok={l.ok === true} class:fail={l.ok === false}>
						<span class="muted">{when(l.at)}</span>
						<span>{l.ok === true ? 'ok' : l.ok === false ? 'failed' : ''}</span>
						<span class="text">{l.text}</span>
					</li>
				{/each}
			</ul>
		</section>
	{/if}
</div>

<style>
	.filters {
		display: flex;
		gap: 14px;
	}
	.walls {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(min(100%, 420px), 1fr));
		gap: 14px;
	}
	.wall {
		position: relative;
		padding: 0;
		border-radius: var(--radius);
		overflow: hidden;
		aspect-ratio: 16 / 9;
		background: var(--panel-2);
		cursor: zoom-in;
		display: block;
		text-align: left;
	}
	.wall img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
		transition: transform 0.3s;
	}
	.wall:hover img {
		transform: scale(1.03);
	}
	.cap {
		position: absolute;
		inset: auto 0 0 0;
		padding: 30px 14px 10px;
		background: linear-gradient(transparent, rgb(8 6 14 / 0.88));
		display: flex;
		flex-direction: column;
	}
	.kicker {
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--accent);
		font-weight: 600;
	}
	.theme {
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.blank {
		position: absolute;
		top: 8px;
		right: 8px;
	}
	.log {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
		max-height: 320px;
		overflow: auto;
	}
	.log li {
		display: grid;
		grid-template-columns: 170px 60px 1fr;
		gap: 8px;
	}
	.log .ok span:nth-child(2) {
		color: var(--green);
	}
	.log .fail span:nth-child(2) {
		color: var(--red);
	}
	.text {
		overflow-wrap: anywhere;
	}
	@media (max-width: 640px) {
		.log li {
			grid-template-columns: 1fr;
			gap: 0;
		}
	}
</style>
