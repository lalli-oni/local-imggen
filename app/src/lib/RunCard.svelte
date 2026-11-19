<script lang="ts">
	import type { Run } from './api';
	import { lightbox, type LightboxItem } from './lightbox.svelte';
	import { seconds } from './format';
	import Thumb from './Thumb.svelte';

	let { run }: { run: Run } = $props();
	const href = $derived(`/runs/${run.id}`);
	const items = $derived(
		run.images
			.filter((im) => im.url)
			.map(
				(im): LightboxItem => ({
					url: im.url!,
					title: `${run.preset} · seed ${im.seed ?? '?'}`,
					caption: run.daily?.theme ?? run.prompt ?? '',
					href
				})
			)
	);
</script>

<article class="run">
	<header>
		<a {href} class="label">{run.daily?.theme ?? run.prompt ?? run.label}</a>
		<span class="meta">
			{run.date} {run.time.slice(0, 5)} · {run.preset}{#if run.styles.length}+{run.styles.join('+')}{/if}
			{#if run.seconds} · {seconds(run.seconds)}{/if}
		</span>
	</header>
	<div class="grid">
		{#each run.images.slice(0, 4) as im, i (im.name)}
			<Thumb
				url={im.url}
				alt={im.name}
				blank={im.blank}
				label={i === 3 && run.images.length > 4 ? `+${run.images.length - 4} more` : ''}
				onclick={() => lightbox.open(items, Math.max(0, items.findIndex((x) => x.url === im.url)))}
			/>
		{/each}
	</div>
</article>

<style>
	.run {
		background: var(--panel);
		border: 1px solid var(--line);
		border-radius: 14px;
		padding: 12px;
		display: flex;
		flex-direction: column;
		gap: 10px;
		min-width: 0;
	}
	header {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.label {
		color: var(--text);
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.meta {
		color: var(--muted);
		font-size: 12px;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 6px;
	}
</style>
