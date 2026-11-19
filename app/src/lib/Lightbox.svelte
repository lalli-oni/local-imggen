<script lang="ts">
	import { lightbox } from './lightbox.svelte';

	let actual = $state(false); // false = fit the screen, true = 1:1 pixels (scroll to pan)

	function onkey(e: KeyboardEvent) {
		if (!lightbox.current) return;
		if (e.key === 'Escape') lightbox.close();
		else if (e.key === 'ArrowRight') lightbox.step(1);
		else if (e.key === 'ArrowLeft') lightbox.step(-1);
		else if (e.key === 'f') actual = !actual;
		else return;
		e.preventDefault();
	}
</script>

<svelte:window onkeydown={onkey} />

{#if lightbox.current}
	{@const item = lightbox.current}
	<div class="lb" role="dialog" aria-modal="true" aria-label={item.title}>
		<button class="backdrop" aria-label="Close" onclick={() => lightbox.close()}></button>
		<div class="stage" class:actual>
			<img src={item.url} alt={item.title} />
		</div>
		<header>
			<div class="title">
				<strong>{item.title}</strong>
				{#if item.caption}<span>{item.caption}</span>{/if}
			</div>
			<div class="tools">
				<span class="count">{lightbox.index + 1} / {lightbox.items.length}</span>
				<button onclick={() => (actual = !actual)} title="f">{actual ? 'Fit' : '1:1'}</button>
				{#if item.href}<a class="btn" href={item.href} onclick={() => lightbox.close()}>Details</a>{/if}
				<a class="btn" href={item.url} target="_blank" rel="noopener">Open</a>
				<button onclick={() => lightbox.close()} aria-label="Close (Esc)">✕</button>
			</div>
		</header>
		{#if lightbox.items.length > 1}
			<button class="nav prev" aria-label="Previous (←)" onclick={() => lightbox.step(-1)}>‹</button>
			<button class="nav next" aria-label="Next (→)" onclick={() => lightbox.step(1)}>›</button>
		{/if}
	</div>
{/if}

<style>
	.lb {
		position: fixed;
		inset: 0;
		z-index: 50;
		display: grid;
		grid-template-rows: auto 1fr;
	}
	.backdrop {
		position: absolute;
		inset: 0;
		background: rgb(5 4 10 / 0.94);
		border: 0;
		border-radius: 0;
		cursor: zoom-out;
	}
	header {
		position: relative;
		grid-row: 1;
		display: flex;
		gap: 12px;
		align-items: center;
		justify-content: space-between;
		padding: 10px 16px;
		background: rgb(13 11 20 / 0.8);
	}
	.title {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.title span {
		color: var(--muted);
		font-size: 13px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.tools {
		display: flex;
		gap: 6px;
		align-items: center;
		flex-shrink: 0;
	}
	.count {
		color: var(--muted);
		font-size: 13px;
		margin-right: 4px;
	}
	.stage {
		position: relative;
		grid-row: 2;
		min-height: 0;
		display: grid;
		place-items: center;
		padding: 12px;
		pointer-events: none;
	}
	.stage img {
		max-width: 100%;
		max-height: 100%;
		object-fit: contain;
		pointer-events: auto;
		box-shadow: 0 10px 60px rgb(0 0 0 / 0.6);
	}
	.stage.actual {
		overflow: auto;
		place-items: start center;
		pointer-events: auto;
	}
	.stage.actual img {
		max-width: none;
		max-height: none;
	}
	.nav {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		font-size: 34px;
		line-height: 1;
		width: 48px;
		height: 72px;
		background: rgb(13 11 20 / 0.6);
	}
	.prev {
		left: 8px;
	}
	.next {
		right: 8px;
	}
</style>
