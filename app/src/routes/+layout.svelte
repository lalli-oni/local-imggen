<script lang="ts">
	import '../app.css';
	import { page, navigating } from '$app/state';
	import Lightbox from '$lib/Lightbox.svelte';

	let { children } = $props();
	const nav = [
		{ href: '/', label: 'Home' },
		{ href: '/projects', label: 'Projects' },
		{ href: '/characters', label: 'Characters' },
		{ href: '/wallpapers', label: 'Wallpapers' },
		{ href: '/runs', label: 'All runs' }
	];
	const active = (href: string) =>
		href === '/' ? page.url.pathname === '/' : page.url.pathname.startsWith(href);
</script>

<div class="shell">
	<header class="top">
		<a href="/" class="brand"><span class="logo">◢◤</span> imagegen</a>
		<nav>
			{#each nav as n (n.href)}
				<a href={n.href} class:active={active(n.href)} aria-current={active(n.href) ? 'page' : undefined}
					>{n.label}</a
				>
			{/each}
		</nav>
		{#if navigating.to}<span class="loading" aria-label="Loading"></span>{/if}
	</header>
	<main>
		{@render children()}
	</main>
</div>
<Lightbox />

<style>
	.shell {
		min-height: 100vh;
	}
	.top {
		position: sticky;
		top: 0;
		z-index: 10;
		display: flex;
		align-items: center;
		gap: 24px;
		padding: 10px 24px;
		background: rgb(13 11 20 / 0.85);
		backdrop-filter: blur(10px);
		border-bottom: 1px solid var(--line);
	}
	.brand {
		color: var(--text);
		font-weight: 700;
		letter-spacing: 0.02em;
		white-space: nowrap;
	}
	.logo {
		background: linear-gradient(90deg, var(--accent), var(--cyan));
		-webkit-background-clip: text;
		background-clip: text;
		color: transparent;
	}
	nav {
		display: flex;
		gap: 4px;
		overflow-x: auto;
	}
	nav a {
		color: var(--muted);
		padding: 6px 12px;
		border-radius: 8px;
		white-space: nowrap;
	}
	nav a:hover {
		color: var(--text);
		background: var(--panel);
		text-decoration: none;
	}
	nav a.active {
		color: var(--text);
		background: var(--panel-2);
	}
	.loading {
		margin-left: auto;
		width: 60px;
		height: 3px;
		border-radius: 3px;
		background: linear-gradient(90deg, var(--accent), var(--cyan), var(--accent));
		background-size: 200% 100%;
		animation: slide 1s linear infinite;
	}
	@keyframes slide {
		to {
			background-position: -200% 0;
		}
	}
	main {
		max-width: 1500px;
		margin: 0 auto;
		padding: 24px;
	}
	@media (max-width: 640px) {
		.top {
			gap: 12px;
			padding: 8px 16px;
		}
		main {
			padding: 16px;
		}
	}
</style>
