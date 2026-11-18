<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, RUNS_PAGE, type Run, type RunsPage } from '$lib/api';
	import RunCard from '$lib/RunCard.svelte';

	let { data } = $props();

	// the first page comes from load(); "Load more" appends the next ones here
	let more = $state<Run[]>([]);
	let loading = $state(false);
	$effect(() => {
		void data.query; // a new filter: start over
		more = [];
	});
	const runs = $derived([...data.page.runs, ...more]);

	let text = $state(page.url.searchParams.get('q') ?? '');
	const preset = $derived(page.url.searchParams.get('preset') ?? '');
	const wallpaper = $derived(page.url.searchParams.get('wallpaper') === '1');

	function setParam(k: string, v: string) {
		const u = new URL(page.url);
		if (v) u.searchParams.set(k, v);
		else u.searchParams.delete(k);
		goto(u, { replaceState: true, keepFocus: true, noScroll: true });
	}

	let timer: ReturnType<typeof setTimeout>;
	function onsearch() {
		clearTimeout(timer);
		timer = setTimeout(() => setParam('q', text.trim()), 250);
	}

	async function loadMore() {
		loading = true;
		try {
			const q = new URLSearchParams(data.query);
			q.set('offset', String(runs.length));
			q.set('limit', String(RUNS_PAGE));
			const next = await api<RunsPage>(fetch, `runs?${q}`);
			more = [...more, ...next.runs];
		} finally {
			loading = false;
		}
	}
</script>

<svelte:head><title>All runs · imagegen</title></svelte:head>

<div class="page">
	<div class="section-head">
		<h1>All runs <span class="muted small">{data.page.total}</span></h1>
	</div>
	<div class="bar">
		<input type="search" placeholder="Search prompts and themes" bind:value={text} oninput={onsearch} aria-label="Search" />
		<select value={preset} onchange={(e) => setParam('preset', e.currentTarget.value)} aria-label="Preset">
			<option value="">All presets</option>
			{#each data.page.presets as p (p)}<option value={p}>{p}</option>{/each}
		</select>
		<label class="small muted">
			<input type="checkbox" checked={wallpaper} onchange={(e) => setParam('wallpaper', e.currentTarget.checked ? '1' : '')} />
			wallpapers only
		</label>
	</div>

	{#if runs.length}
		<div class="cards">
			{#each runs as run (run.id)}<RunCard {run} />{/each}
		</div>
		{#if runs.length < data.page.total}
			<button class="more" onclick={loadMore} disabled={loading}>
				{loading ? 'Loading…' : `Load more (${data.page.total - runs.length} left)`}
			</button>
		{/if}
	{:else}
		<p class="empty">No runs match.</p>
	{/if}
</div>

<style>
	.bar {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		align-items: center;
		margin-top: -12px;
	}
	.bar input[type='search'] {
		flex: 1 1 260px;
	}
	.more {
		align-self: center;
		padding: 8px 18px;
	}
</style>
