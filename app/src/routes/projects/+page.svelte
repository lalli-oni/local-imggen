<script lang="ts">
	import ProjectTile from '$lib/ProjectTile.svelte';

	let { data } = $props();
</script>

<svelte:head><title>Projects · imagegen</title></svelte:head>

<div class="page">
	<h1>Projects</h1>
	{#if data.projects.length}
		<div class="cards">
			{#each data.projects as p (p.slug)}<ProjectTile {p} />{/each}
		</div>
	{:else}
		<p class="empty">No projects yet. Start one with <code>gen project new SLUG --prompt "..."</code></p>
	{/if}
</div>
