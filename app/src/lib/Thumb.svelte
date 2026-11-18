<script lang="ts">
	import type { Rating } from './api';

	let {
		url,
		alt,
		label = '',
		rating = null,
		blank = false,
		onclick
	}: {
		url: string | null;
		alt: string;
		label?: string;
		rating?: Rating;
		blank?: boolean;
		onclick?: () => void;
	} = $props();
</script>

<button class="thumb {rating ?? ''}" {onclick} disabled={!url} title={alt}>
	{#if url}
		<img src={url} {alt} loading="lazy" decoding="async" />
	{:else}
		<span class="missing">missing</span>
	{/if}
	{#if label || rating || blank}
		<span class="tag">
			{#if label}{label}{/if}
			{#if rating}<b>{rating}</b>{/if}
			{#if blank}<b class="blank">blank</b>{/if}
		</span>
	{/if}
</button>

<style>
	.thumb {
		position: relative;
		display: block;
		width: 100%;
		aspect-ratio: var(--thumb-ratio, 1);
		padding: 0;
		overflow: hidden;
		border-radius: 10px;
		background: var(--panel-2);
		border: 2px solid transparent;
		cursor: zoom-in;
	}
	.thumb:hover {
		border-color: var(--line-strong);
	}
	img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.pick {
		border-color: var(--green);
	}
	.maybe {
		border-color: var(--gold);
	}
	.reject {
		opacity: 0.45;
	}
	.tag {
		position: absolute;
		left: 6px;
		bottom: 6px;
		display: flex;
		gap: 6px;
		padding: 2px 7px;
		border-radius: 6px;
		background: rgb(10 8 16 / 0.75);
		font-size: 12px;
		color: var(--text);
	}
	.tag b {
		font-weight: 600;
		text-transform: uppercase;
		font-size: 11px;
	}
	.pick .tag b {
		color: var(--green);
	}
	.maybe .tag b {
		color: var(--gold);
	}
	.reject .tag b,
	.blank {
		color: var(--red);
	}
	.missing {
		color: var(--muted);
		font-size: 12px;
	}
</style>
