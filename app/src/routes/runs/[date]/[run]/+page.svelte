<script lang="ts">
	import Thumb from '$lib/Thumb.svelte';
	import { lightbox, type LightboxItem } from '$lib/lightbox.svelte';
	import { day, seconds } from '$lib/format';

	let { data } = $props();
	const r = $derived(data.run);
	const items = $derived(
		r.images
			.filter((im) => im.url)
			.map((im): LightboxItem => ({ url: im.url!, title: `${im.name} · seed ${im.seed ?? '?'}`, caption: im.prompt ?? r.prompt ?? '' }))
	);
	const first = $derived(r.images[0]);
	const params = $derived(
		Object.entries(first?.params ?? {}).filter(([, v]) => v !== null && typeof v !== 'object')
	);
</script>

<svelte:head><title>{r.label} · imagegen</title></svelte:head>

<div class="page">
	<header class="head">
		<a href="/runs" class="small">All runs</a>
		<h1>{r.daily?.theme ?? r.prompt ?? r.label}</h1>
		<div class="chips">
			<span class="chip">{day(r.date)} {r.time}</span>
			<span class="chip">{r.preset}</span>
			{#each r.styles as s (s)}<span class="chip">{s}</span>{/each}
			{#if r.daily}<span class="chip warn">daily wallpaper</span>{/if}
			{#if r.seconds}<span class="chip">{seconds(r.seconds)}</span>{/if}
			{#if r.sheet}<a class="btn" href={r.sheet} target="_blank" rel="noopener">Contact sheet</a>{/if}
		</div>
	</header>

	<div class="thumbs" style="--thumb: 300px">
		{#each r.images as im (im.name)}
			<figure>
				<Thumb
					url={im.url}
					alt={im.name}
					blank={im.blank}
					onclick={() => lightbox.open(items, Math.max(0, items.findIndex((x) => x.url === im.url)))}
				/>
				<figcaption class="small muted">
					seed {im.seed ?? '?'}{#if im.timing} · {seconds(im.timing)}{/if}
					{#if im.scene}· <a href={im.scene} target="_blank" rel="noopener">scene</a>{/if}
				</figcaption>
			</figure>
		{/each}
	</div>

	<div class="grid2">
		<section class="panel section">
			<h3>Prompt</h3>
			<pre class="prompt">{r.prompt}</pre>
			{#if first?.full_prompt && first.full_prompt !== r.prompt}
				<h3>Full prompt</h3>
				<pre class="prompt">{first.full_prompt}</pre>
			{/if}
			{#if first?.negative}
				<h3>Negative</h3>
				<pre class="prompt">{first.negative}</pre>
			{/if}
		</section>
		<section class="panel section">
			{#if r.daily}
				<h3>Daily</h3>
				<p class="small">
					Theme <b>{r.daily.theme}</b>{#if r.daily.from} (from {r.daily.from}){/if}
					{#if r.daily.props?.length}<br />Props: {r.daily.props.join(', ')}{/if}
					{#if r.daily.cast}<br />Cast: {Object.keys(r.daily.cast).join(', ')}{/if}
				</p>
			{/if}
			{#if params.length}
				<h3>Settings</h3>
				<dl>
					{#each params as [k, v] (k)}<dt>{k}</dt><dd>{v}</dd>{/each}
				</dl>
			{/if}
			{#if r.timing}
				<h3>Timing</h3>
				<p class="small">{r.timing}</p>
			{/if}
			{#if r.source}
				<h3>Source image</h3>
				<a href={r.source} target="_blank" rel="noopener"><img class="source" src={r.source} alt="source" /></a>
			{/if}
		</section>
	</div>
</div>

<style>
	.head {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	figure {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.grid2 {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr));
		gap: 14px;
		align-items: start;
	}
	dl {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 2px 14px;
		margin: 0;
		font-size: 13px;
	}
	dt {
		color: var(--muted);
	}
	dd {
		margin: 0;
		overflow-wrap: anywhere;
	}
	.source {
		max-width: 200px;
		border-radius: 8px;
	}
</style>
